#!/usr/bin/env python3
"""Fail when the checked-in OpenAPI spec drifts from the code.

`openapi/openapi.json` is a checked-in artifact; this script regenerates the
spec from `create_app()` into a temporary path and diffs it against the
checked-in file, exiting 1 on any difference. The spec is a projection, not a
second source of truth: this check is code -> spec only, so a stale spec is a
CI failure instead of a silent contract divergence.

With --strict-coverage it additionally fails if any JSON operation still
declares the empty response schema FastAPI emits for untyped operations
(content: {"application/json": {"schema": {}}}). A covered response is a
`$ref`, an object schema with `properties`, or a composite (`anyOf`/`oneOf`)
schema — the latter is how discriminated unions (typed model variants selected
by a discriminator) are emitted. The flag is wired into CI in the phase that
lands the last response models (plan Phase 4) so that from then on any new
untyped endpoint breaks the build; until then the flag exists but is opt-in.

No services are contacted (see dump_openapi.py).
"""

import argparse
import difflib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKED_IN = ROOT / "openapi" / "openapi.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _uncovered_json_responses(spec: dict) -> list[str]:
    """List `METHOD /path -> status` responses whose JSON schema is the empty
    schema FastAPI emits for untyped operations."""
    covered_keys = ("$ref", "properties", "anyOf", "oneOf")
    uncovered = []
    for path, item in sorted(spec.get("paths", {}).items()):
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            for status, response in operation.get("responses", {}).items():
                if not str(status).startswith("2"):
                    continue
                media = response.get("content", {}).get("application/json")
                if media is None:
                    continue
                schema = media.get("schema") or {}
                if schema and any(key in schema for key in covered_keys):
                    continue
                uncovered.append(f"{method.upper()} {path} -> {status}")
    return uncovered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help="also fail when a 2xx JSON response has an empty (untyped) schema",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dump_openapi

    if not CHECKED_IN.exists():
        print(f"FAIL: {CHECKED_IN.relative_to(ROOT)} is not checked in; "
              f"run scripts/dump_openapi.py and commit it", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="openapi-check-") as tmp:
        generated_text = dump_openapi.dump(Path(tmp) / "openapi.json").read_text()
    checked_text = CHECKED_IN.read_text()

    if generated_text != checked_text:
        print(
            f"FAIL: {CHECKED_IN.relative_to(ROOT)} has drifted from the code. "
            "Regenerate with scripts/dump_openapi.py, review the diff, and "
            "commit both in the same change.",
            file=sys.stderr,
        )
        for line in difflib.unified_diff(
            checked_text.splitlines(keepends=True),
            generated_text.splitlines(keepends=True),
            f"{CHECKED_IN.relative_to(ROOT)} (checked in)",
            "<regenerated>",
        ):
            sys.stderr.write(line)
        return 1

    if args.strict_coverage:
        spec = json.loads(generated_text)
        uncovered = _uncovered_json_responses(spec)
        if uncovered:
            print(
                "FAIL: --strict-coverage: these JSON responses are still untyped "
                "(empty schema):",
                file=sys.stderr,
            )
            for entry in uncovered:
                print(f"  {entry}", file=sys.stderr)
            return 1

    print(f"OK: {CHECKED_IN.relative_to(ROOT)} matches the code")
    if args.strict_coverage:
        print("OK: all 2xx JSON responses carry a response schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
