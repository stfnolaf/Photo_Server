"""The photo-upload CLI: upload photos to a Photo Server over HTTP.

Phase 6 of docs/openapi-codegen-plan.md: every JSON exchange with the API
is validated through the Pydantic models generated from the checked-in
OpenAPI spec (``photo_server/generated``, regenerated with ``npm run
generate:api`` in ``backend/``), so the CLI, the web app, and the server all
read the same contract through the same generator ecosystem. A server
response that drifts from the spec (a key the model does not declare, a
missing required key, a wrong type) fails loudly here as a contract
violation instead of surfacing later as a ``KeyError`` deep in the flow.

The transport stays hand-written: the generated ``Sdk``'s method stubs take
no parameters in @hey-api/openapi-python 0.0.24, and this CLI's PUT sends a
raw binary body to the per-file batch endpoint — a plain ``httpx.Client``
with the spec's relative URLs covers both. The final stdout is the raw wire
JSON of the last response (server key order, byte-identical to the
pre-Phase-6 CLI for the same server output).
"""

import argparse
import json
import mimetypes
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from photo_server.generated.pydantic_gen import (
    UploadBatchOut,
    UploadBatchOutStatus,
    UploadBatchRequest,
    UploadFileOut,
    UploadFileOutStatus,
    UploadFileReceipt,
)
from photo_server.selection import MEDIA

SUPPORTED = MEDIA | {".xmp"}


def _files(root: Path, inputs: list[str], recursive: bool) -> list[Path]:
    root = root.resolve(strict=True)
    found: set[Path] = set()
    for value in inputs:
        candidate = Path(value)
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve(
            strict=True
        )
        if not candidate.is_relative_to(root):
            raise ValueError(f"Input is outside --root: {value}")
        if candidate.is_dir():
            if not recursive:
                raise ValueError(f"Use --recursive to upload a directory: {value}")
            found.update(
                path.resolve()
                for path in candidate.rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED
            )
        elif candidate.is_file():
            found.add(candidate)
        else:
            raise ValueError(f"Input is not a regular file: {value}")
    if not found:
        raise ValueError("No supported photos or XMP sidecars were found")
    unsupported = [path for path in found if path.suffix.lower() not in SUPPORTED]
    if unsupported:
        raise ValueError(f"Unsupported file extension: {unsupported[0].name}")
    return sorted(found, key=lambda path: path.relative_to(root).as_posix())


def _body(response: httpx.Response) -> str:
    try:
        return json.dumps(response.json())
    except ValueError:
        return response.text


def _check(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        raise RuntimeError(f"Server returned HTTP {response.status_code}: {_body(response)}")
    return response


def _validate(model: type, payload: Any, source: str = "Server response"):
    """Validate a wire JSON payload through a generated model. A contract
    violation fails loudly (plan decision 3): the model is the declaration
    and the enforcement."""
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise RuntimeError(f"{source} violates the OpenAPI contract: {error}") from error


def _batch_response(response: httpx.Response) -> tuple[UploadBatchOut, dict]:
    """Validate a ``describe_batch`` response; return the model plus the raw
    wire dict (for the CLI's byte-faithful final stdout)."""
    payload = _check(response).json()
    return _validate(UploadBatchOut, payload), payload


def upload(
    server: str,
    root: Path,
    inputs: list[str],
    recursive: bool,
    parallel: int,
    batch_id: UUID,
    wait: bool,
) -> tuple[UploadBatchOut, dict]:
    """Upload ``inputs`` under ``root`` to ``server`` as batch ``batch_id``.

    Returns the validated final batch model plus the raw wire dict of the
    response it was parsed from.
    """
    paths = _files(root, inputs, recursive)
    root = root.resolve()
    local_by_relative = {path.relative_to(root).as_posix(): path for path in paths}
    declaration_payload = {
        "batchId": str(batch_id),
        "files": [
            {
                "path": relative,
                "sizeBytes": path.stat().st_size,
                "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            }
            for relative, path in local_by_relative.items()
        ],
    }
    # Validate the outgoing declaration through the generated request model
    # (the same model the server validates it against) so a local drift from
    # the spec fails before the first byte is sent; the raw dict is what is
    # actually sent, so the request bytes are unchanged.
    _validate(UploadBatchRequest, declaration_payload, source="Outgoing declaration")
    timeout = httpx.Timeout(connect=10, read=300, write=300, pool=300)
    with httpx.Client(base_url=server.rstrip("/"), timeout=timeout) as client:
        batch, _ = _batch_response(client.post("/upload-batches", json=declaration_payload))
        required = [
            file
            for file in batch.files
            if file.required and file.status == UploadFileOutStatus.WAITING
        ]

        def send(file: UploadFileOut) -> UploadFileReceipt:
            path = local_by_relative[file.path]
            with path.open("rb") as stream:
                response = client.put(
                    file.upload_url,
                    content=stream,
                    headers={"Content-Type": file.mime_type or "application/octet-stream"},
                )
            return _validate(UploadFileReceipt, _check(response).json())

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(send, file): file.path for file in required}
            complete = 0
            for future in as_completed(futures):
                try:
                    future.result()
                    complete += 1
                    print(f"Uploaded {complete}/{len(required)}", file=sys.stderr, end="\r")
                except Exception as error:
                    raise RuntimeError(f"Upload failed for {futures[future]}: {error}") from error
            if required:
                print(file=sys.stderr)

        batch, raw = _batch_response(client.post(f"/upload-batches/{batch_id}/seal"))
        if wait:
            while batch.status in {UploadBatchOutStatus.QUEUED, UploadBatchOutStatus.PROCESSING}:
                time.sleep(1)
                batch, raw = _batch_response(client.get(f"/upload-batches/{batch_id}"))
        return batch, raw


def main():
    parser = argparse.ArgumentParser(description="Upload photos to Photo Server over HTTP")
    parser.add_argument("server", help="Photo Server URL, for example http://SERVER_IP:8000")
    parser.add_argument("paths", nargs="+", help="Files, or directories with --recursive")
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="Root for relative upload paths"
    )
    parser.add_argument(
        "--recursive", action="store_true", help="Recursively enumerate directories"
    )
    parser.add_argument("--parallel", type=int, default=8, choices=range(1, 33))
    parser.add_argument("--batch-id", type=UUID, default=uuid4())
    parser.add_argument("--wait", action="store_true", help="Wait for background onboarding")
    args = parser.parse_args()
    print(f"Batch ID (retain for retries): {args.batch_id}", file=sys.stderr)
    try:
        result, raw = upload(
            args.server,
            args.root,
            args.paths,
            args.recursive,
            args.parallel,
            args.batch_id,
            args.wait,
        )
        print(json.dumps(raw, indent=2))
        if result.status == UploadBatchOutStatus.FAILED:
            raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        print(json.dumps({"batchId": str(args.batch_id), "error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
