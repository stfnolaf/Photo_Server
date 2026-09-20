#!/usr/bin/env python3
"""Regenerate the checked-in OpenAPI spec from the running code.

`openapi/openapi.json` is a projection of `photo_server.api.create_app()`, never
a second source of truth: the Pydantic models in the code are the contract, and
this script makes the spec a reproducible artifact of them. After changing any
request/response shape, run this script, review the diff, and commit the spec in
the same change (`scripts/check_openapi.py` fails on drift).

No services are contacted: building the app only constructs clients and the
database engine, it does not open connections. When run without a real
deployment environment, dummy settings are injected so the spec can be produced
on a fresh clone; a real environment (env vars or .env) always takes precedence.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "openapi" / "openapi.json"

# Values are never used for a connection; they only satisfy Settings so the
# app can be constructed anywhere. os.environ.setdefault keeps a real
# environment (or .env) authoritative when one is present.
DUMMY_ENV = {
    "PHOTO_S3_ENDPOINT": "http://127.0.0.1:9",
    "PHOTO_S3_BUCKET": "photo-spec-dump",
    "PHOTO_S3_ANONYMOUS": "true",
    "PHOTO_DATABASE_URL": "postgresql+psycopg://photo:photo@127.0.0.1:55432/photo",
}


def _bootstrap_import():
    sys.path.insert(0, str(ROOT / "backend" / "src"))
    for name, value in DUMMY_ENV.items():
        os.environ.setdefault(name, value)
    from photo_server.api import create_app  # noqa: E402

    return create_app()


def generate_spec() -> dict:
    return _bootstrap_import().openapi()


def render(spec: dict) -> str:
    # Sorted keys + fixed indentation make the file diff-stable across runs.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def dump(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(generate_spec()))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the spec (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    spec = generate_spec()
    if args.output == DEFAULT_OUTPUT:
        dump(args.output)
        print(f"wrote {args.output.relative_to(ROOT)}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render(spec))
        print(f"wrote {args.output}")

    paths = spec.get("paths", {})
    operations = sum(
        1
        for item in paths.values()
        for method in item
        if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
    )
    print(f"{len(paths)} paths, {operations} operations")


if __name__ == "__main__":
    main()
