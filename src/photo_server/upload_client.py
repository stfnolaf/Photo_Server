import argparse
import json
import mimetypes
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import UUID, uuid4

import httpx

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


def upload(
    server: str,
    root: Path,
    inputs: list[str],
    recursive: bool,
    parallel: int,
    batch_id: UUID,
    wait: bool,
) -> dict:
    paths = _files(root, inputs, recursive)
    root = root.resolve()
    local_by_relative = {path.relative_to(root).as_posix(): path for path in paths}
    declaration = {
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
    timeout = httpx.Timeout(connect=10, read=300, write=300, pool=300)
    with httpx.Client(base_url=server.rstrip("/"), timeout=timeout) as client:
        batch = _check(client.post("/upload-batches", json=declaration)).json()
        required = [
            file for file in batch["files"] if file["required"] and file["status"] == "waiting"
        ]

        def send(file: dict) -> dict:
            path = local_by_relative[file["path"]]
            with path.open("rb") as stream:
                response = client.put(
                    file["uploadUrl"],
                    content=stream,
                    headers={"Content-Type": file["mimeType"] or "application/octet-stream"},
                )
            return _check(response).json()

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(send, file): file["path"] for file in required}
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

        batch = _check(client.post(f"/upload-batches/{batch_id}/seal")).json()
        if wait:
            while batch["status"] in {"queued", "processing"}:
                time.sleep(1)
                batch = _check(client.get(f"/upload-batches/{batch_id}")).json()
        return batch


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
        result = upload(
            args.server,
            args.root,
            args.paths,
            args.recursive,
            args.parallel,
            args.batch_id,
            args.wait,
        )
        print(json.dumps(result, indent=2))
        if result["status"] == "failed":
            raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        print(json.dumps({"batchId": str(args.batch_id), "error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
