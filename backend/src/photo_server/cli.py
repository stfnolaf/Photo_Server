import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

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
    commands.add_parser(
        "recluster-bursts",
        help="Rebuild all display burst memberships using the current thresholds",
    )
    refresh = commands.add_parser(
        "refresh-metadata", help="Queue metadata reprocessing from immutable originals"
    )
    refresh.add_argument(
        "--asset", type=UUID, action="append", help="Asset to process; repeat for many"
    )
    refresh.add_argument(
        "--include-deleted", action="store_true", help="Include hidden when processing the library"
    )
    fingerprint = commands.add_parser(
        "backfill-fingerprints", help="Queue preview-independent burst fingerprint processing"
    )
    fingerprint.add_argument(
        "--asset", type=UUID, action="append", help="Asset to process; repeat for many"
    )
    fingerprint.add_argument(
        "--include-deleted", action="store_true", help="Include hidden when processing the library"
    )
    analyze = commands.add_parser(
        "analyze", help="Queue local face and semantic analysis"
    )
    analyze.add_argument("--asset", type=UUID, action="append", help="Asset to analyze; repeat for many")
    analyze.add_argument(
        "--include-deleted", action="store_true", help="Include hidden when analyzing the library"
    )
    analyze.add_argument(
        "--force-full",
        action="store_true",
        help="Bypass semantic reuse for the queued analysis jobs",
    )
    export = commands.add_parser(
        "export", help="Export the PostgreSQL catalog and its original S3 objects"
    )
    export.add_argument("destination", type=Path)
    export.add_argument(
        "--include-trash", action="store_true", help="Also export hidden originals"
    )
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true", help="Process at most one queued job")
    worker.add_argument(
        "--mode",
        choices=("all", "onboarding", "processing", "preview"),
        default="all",
        help="Queue lane to serve (default: all; use dedicated Compose workers for isolation)",
    )
    ai_worker = commands.add_parser("ai-worker")
    ai_worker.add_argument("--once", action="store_true", help="Analyze at most one queued asset")
    commands.add_parser(
        "cache-rebuild-index",
        help="Backfill preview_cache rows for cached preview sets that predate tracking",
    )
    args = parser.parse_args()
    settings = Settings()
    try:
        if args.command == "export":
            result = export_library(settings, args.destination, args.include_trash)
        else:
            service = Service(settings)
            initialized = service.initialize(
                recover_uploads=args.command not in {"worker", "ai-worker", "migrate", "cache-rebuild-index"}
            )
            if args.command in {"init", "migrate"}:
                result = initialized
            elif args.command == "verify":
                result = service.verify(args.full)
            elif args.command == "list":
                result = service.catalog.list_assets()
            elif args.command == "recluster-bursts":
                result = service.catalog.recluster_bursts()
            elif args.command == "refresh-metadata":
                result = service.queue_processing(
                    args.asset,
                    ["metadata"],
                    args.include_deleted,
                )
            elif args.command == "backfill-fingerprints":
                result = service.queue_processing(
                    args.asset,
                    ["fingerprint"],
                    args.include_deleted,
                )
            elif args.command == "analyze":
                result = service.queue_analysis(
                    args.asset, args.include_deleted, args.force_full
                )
            elif args.command == "ai-worker":
                from photo_server.ai_worker import run as run_ai

                result = run_ai(service, args.once)
                if not args.once:
                    return
            elif args.command == "cache-rebuild-index":
                from photo_server.worker import rebuild_cache_index

                result = rebuild_cache_index(service)
            else:
                from photo_server.worker import run, run_once

                if args.once:
                    result = run_once(service, args.mode) or {"status": "idle"}
                else:
                    run(service, args.mode)
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
