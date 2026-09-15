import argparse
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from photo_server.config import Settings
from photo_server.export import export_library
from photo_server.service import Service


def main():
    parser = argparse.ArgumentParser(
        description="RAW-first library. Imports accept explicit files only."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the library bucket/marker and database schema")
    for command in ("plan", "import"):
        child = commands.add_parser(command)
        child.add_argument(
            "files",
            nargs="+",
            help="Files relative to PHOTO_IMPORT_ROOT (or absolute paths inside it)",
        )
        if command == "import":
            child.add_argument(
                "--operation-id", type=UUID, help="Reuse this ID when retrying an interrupted batch"
            )
    recover = commands.add_parser("recover", help="Rebuild/reconcile PostgreSQL from S3 manifests")
    recover.add_argument(
        "--verify",
        action="store_true",
        help="Download and hash every blob, in addition to checking its size",
    )
    commands.add_parser("list", help="List up to 100 indexed assets")
    export = commands.add_parser(
        "export", help="Export originals/manifests directly from S3 without PostgreSQL"
    )
    export.add_argument("destination", type=Path)
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true", help="Process at most one preview job")
    args = parser.parse_args()
    settings = Settings()
    try:
        if args.command == "export":
            result = export_library(settings, args.destination)
        else:
            service = Service(settings)
            if args.command == "plan":
                result = service.plan(args.files)
            else:
                initialized = service.initialize()
                if args.command == "init":
                    result = initialized
                elif args.command == "import":
                    operation_id = args.operation_id or uuid4()
                    print(f"Operation ID (retain for retries): {operation_id}", file=sys.stderr)
                    result = service.import_batch(args.files, operation_id)
                elif args.command == "recover":
                    result = service.recover(args.verify)
                elif args.command == "list":
                    result = service.catalog.list_assets()
                else:
                    from photo_server.worker import run, run_once

                    recovery = service.recover()
                    if recovery["errors"]:
                        raise RuntimeError(f"Recovery errors: {recovery['errors']}")
                    if args.once:
                        result = run_once(service) or {"status": "idle"}
                    else:
                        run(service)
                        return
        print(json.dumps(result, indent=2))
        if isinstance(result, dict) and (
            result.get("errors")
            or result.get("status") == "failed"
            or any(
                item["status"] in {"failed", "not_attempted"} for item in result.get("results", [])
            )
        ):
            raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
