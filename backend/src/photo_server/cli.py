import argparse
import json
import sys
from pathlib import Path

from photo_server.config import Settings
from photo_server.export import export_library
from photo_server.service import Service


def main():
    parser = argparse.ArgumentParser(description="RAW-first photo library server administration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the library bucket/marker and database schema")
    commands.add_parser("migrate", help="Apply pending PostgreSQL migrations")
    verify = commands.add_parser("verify", help="Verify PostgreSQL's referenced S3 blobs")
    verify.add_argument(
        "--full",
        action="store_true",
        help="Download and hash every blob, in addition to checking its size",
    )
    commands.add_parser("list", help="List up to 100 indexed assets")
    export = commands.add_parser(
        "export", help="Export the PostgreSQL catalog and its original S3 objects"
    )
    export.add_argument("destination", type=Path)
    export.add_argument(
        "--include-trash", action="store_true", help="Also export trashed originals"
    )
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true", help="Process at most one queued job")
    args = parser.parse_args()
    settings = Settings()
    try:
        if args.command == "export":
            result = export_library(settings, args.destination, args.include_trash)
        else:
            service = Service(settings)
            initialized = service.initialize(
                recover_uploads=args.command not in {"worker", "migrate"}
            )
            if args.command in {"init", "migrate"}:
                result = initialized
            elif args.command == "verify":
                result = service.verify(args.full)
            elif args.command == "list":
                result = service.catalog.list_assets()
            else:
                from photo_server.worker import run, run_once

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
