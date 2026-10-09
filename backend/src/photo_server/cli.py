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
    rebuild = commands.add_parser(
        "rebuild-from-s3", help="Rebuild an empty PostgreSQL projection from canonical S3 records"
    )
    rebuild.add_argument("--checkpoint-id", default="default")
    rebuild.add_argument("--no-resume", action="store_true", help="Ignore the saved scan checkpoint")
    compare = commands.add_parser(
        "compare-s3", help="Rebuild a comparison PostgreSQL projection and compare it with the source"
    )
    compare.add_argument("target_database_url", help="Disposable PostgreSQL URL for the comparison projection")
    compare.add_argument("--checkpoint-id", default="compare")
    reconcile = commands.add_parser(
        "reconcile-s3", aliases=["reconcile"],
        help="Scan immutable S3 state against the PostgreSQL durable projection",
    )
    reconcile.add_argument("--checkpoint-id", default="reconcile")
    reconcile.add_argument("--no-resume", action="store_true")
    reconcile.add_argument("--dry-run", action="store_true", help="Do not mutate S3 checkpoints or PostgreSQL")
    reconcile.add_argument("--apply", action="store_true", help="Repair missing derived PostgreSQL rows")
    reconcile.add_argument("--report-only", action="store_true", help="Report discrepancies without repairs")
    reconcile.add_argument("--stop-after", type=int)
    gc = commands.add_parser(
        "garbage-collect", aliases=["gc-s3"],
        help="Produce a fail-closed dry-run object retention report (never deletes)",
    )
    gc.add_argument("--checkpoint-id", default="garbage-collection")
    gc.add_argument("--no-resume", action="store_true")
    gc.add_argument("--stop-after", type=int)
    gc.add_argument("--as-of", help="UTC RFC3339 evaluation time, useful for repeatable reports")
    gc.add_argument("--deleted-days", type=int, default=30)
    gc.add_argument("--historical-revision-days", type=int, default=90)
    gc.add_argument("--tombstone-days", type=int, default=90)
    gc.add_argument("--original-object-days", type=int, default=30)
    gc.add_argument("--processing-artifact-days", type=int, default=30)
    gc.add_argument("--temporary-upload-days", type=int, default=2)
    recovery = commands.add_parser(
        "create-recovery-checkpoint", help="Create an immutable, verified S3 recovery checkpoint"
    )
    recovery.add_argument("--checkpoint-id", required=True)
    recovery.add_argument("--destination-endpoint", required=True)
    recovery.add_argument("--destination-bucket", required=True)
    recovery.add_argument("--no-resume", action="store_true")
    recovery.add_argument("--stop-after", type=int)
    recovery.add_argument("--without-postgres-dump", action="store_true")
    verify_recovery = commands.add_parser("verify-recovery-checkpoint")
    verify_recovery.add_argument("checkpoint_key")
    verify_recovery.add_argument("--checkpoint-endpoint", required=True)
    verify_recovery.add_argument("--checkpoint-bucket", required=True)
    restore = commands.add_parser("restore-recovery-checkpoint")
    restore.add_argument("checkpoint_key")
    restore.add_argument("--checkpoint-endpoint", required=True)
    restore.add_argument("--checkpoint-bucket", required=True)
    restore.add_argument("--destination-endpoint", required=True)
    restore.add_argument("--destination-bucket", required=True)
    restore.add_argument("--database-url", help="Fresh PostgreSQL URL for restoring the optional derived-index dump")
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
        "refresh-metadata", help="Queue metadata reprocessing from canonical objects"
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
    storage_report = commands.add_parser(
        "storage-report", help="Read-only S3, local derived-storage, and capacity report"
    )
    storage_report.add_argument("--format", choices=("json", "text"), default="json")
    storage = commands.add_parser("storage", help="Storage administration")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_report_nested = storage_commands.add_parser("report", help=storage_report.description)
    storage_report_nested.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    settings = Settings()
    try:
        if args.command == "rebuild-from-s3":
            from photo_server.catalog import Catalog
            from photo_server.rebuild import discover_library_id, rebuild_from_s3
            from photo_server.storage import Storage

            storage = Storage(settings)
            marker = storage.get_json("library.json") if storage.head("library.json") else None
            library_id = marker.get("libraryId") if marker else None
            if not library_id:
                discovered = discover_library_id(storage)
                library_id = str(discovered) if discovered else None
            if not library_id:
                raise ValueError("S3 library identity is missing from the marker and manifests")
            catalog = Catalog(settings.database_url, settings)
            catalog.initialize(library_id)
            result = rebuild_from_s3(
                storage,
                catalog,
                checkpoint_id=args.checkpoint_id,
                resume=not args.no_resume,
            )
        elif args.command == "compare-s3":
            from photo_server.catalog import Catalog
            from photo_server.rebuild import compare_projections, rebuild_from_s3
            from photo_server.storage import Storage

            source = Catalog(settings.database_url, settings)
            library_id = source.existing_library_id()
            if library_id is None:
                raise ValueError("source PostgreSQL library identity is missing")
            target = Catalog(args.target_database_url, settings)
            target.initialize(str(library_id))
            result = rebuild_from_s3(Storage(settings), target, checkpoint_id=args.checkpoint_id, resume=False)
            result["comparison"] = compare_projections(source, target)
            if not result["comparison"]["match"]:
                result["errors"] = result.get("errors", []) + [{"reason": "projection mismatch"}]
        elif args.command in {"reconcile-s3", "reconcile"}:
            from photo_server.catalog import Catalog
            from photo_server.reconcile import reconcile_s3_to_postgres
            from photo_server.storage import Storage

            result = reconcile_s3_to_postgres(
                Storage(settings),
                Catalog(settings.database_url, settings),
                checkpoint_id=args.checkpoint_id,
                resume=not args.no_resume,
                dry_run=args.dry_run or not args.apply,
                apply=args.apply,
                report_only=args.report_only,
                stop_after=args.stop_after,
            )
        elif args.command in {"garbage-collect", "gc-s3"}:
            from photo_server.garbage_collector import RetentionPolicy, collect_garbage
            from photo_server.storage import Storage

            result = collect_garbage(
                Storage(settings),
                policy=RetentionPolicy(
                    deleted_days=args.deleted_days,
                    historical_revision_days=args.historical_revision_days,
                    tombstone_days=args.tombstone_days,
                    original_object_days=args.original_object_days,
                    processing_artifact_days=args.processing_artifact_days,
                    temporary_upload_days=args.temporary_upload_days,
                ),
                checkpoint_id=args.checkpoint_id,
                resume=not args.no_resume,
                stop_after=args.stop_after,
                as_of=args.as_of,
            )
        elif args.command in {"create-recovery-checkpoint", "verify-recovery-checkpoint", "restore-recovery-checkpoint"}:
            from photo_server.recovery import (
                create_recovery_checkpoint,
                restore_recovery_checkpoint,
                verify_recovery_checkpoint,
            )
            from photo_server.storage import Storage

            source = Storage(settings)
            if args.command == "verify-recovery-checkpoint":
                checkpoint_settings = settings.model_copy(
                    update={"s3_endpoint": args.checkpoint_endpoint, "s3_bucket": args.checkpoint_bucket}
                )
                result = verify_recovery_checkpoint(Storage(checkpoint_settings), checkpoint_key=args.checkpoint_key)
            else:
                destination_settings = settings.model_copy(
                    update={
                        "s3_endpoint": args.destination_endpoint,
                        "s3_bucket": args.destination_bucket,
                    }
                )
                destination = Storage(destination_settings)
                destination.ensure_bucket()
                if args.command == "create-recovery-checkpoint":
                    result = create_recovery_checkpoint(
                        source,
                        destination,
                        args.checkpoint_id,
                        database_url=None if args.without_postgres_dump else settings.database_url,
                        checkpoint_prefix=settings.recovery_checkpoint_prefix,
                        progress_prefix=settings.recovery_progress_prefix,
                        resume=not args.no_resume,
                        stop_after=args.stop_after,
                        source_endpoint=settings.s3_endpoint,
                    )
                else:
                    checkpoint_settings = settings.model_copy(
                        update={"s3_endpoint": args.checkpoint_endpoint, "s3_bucket": args.checkpoint_bucket}
                    )
                    checkpoint_source = Storage(checkpoint_settings)
                    result = restore_recovery_checkpoint(checkpoint_source, destination, args.checkpoint_key)
                    if result.get("status") == "complete" and args.database_url:
                        import subprocess

                        def restore_dump(body):
                            subprocess.run(
                                ["pg_restore", "--dbname", args.database_url, "--no-owner", "--no-acl"],
                                input=body, check=True, capture_output=True,
                            )

                        from photo_server.recovery import restore_postgres_dump

                        dump_result = restore_postgres_dump(checkpoint_source, args.checkpoint_key, restore_dump)
                        if dump_result.get("status") != "complete":
                            result = dump_result
        elif args.command == "export":
            result = export_library(settings, args.destination, args.include_trash)
        elif args.command == "storage-report" or (
            args.command == "storage" and args.storage_command == "report"
        ):
            from photo_server.catalog import Catalog
            from photo_server.storage import Storage
            from photo_server.storage_report import format_storage_report
            from photo_server.storage_report import storage_report as make_report

            # Reporting must not create buckets, migrations, markers, or
            # cleanup checkpoints.
            catalog = Catalog(settings.database_url, settings)
            result = make_report(Storage(settings), settings, catalog)
            if args.format == "text":
                print(format_storage_report(result))
                return
        else:
            service = Service(settings)
            initialized = service.initialize(
                recover_uploads=args.command not in {"worker", "ai-worker", "migrate", "cache-rebuild-index"},
                rebuild_projection=args.command not in {"worker", "ai-worker"},
            )
            if args.command in {"init", "migrate"}:
                result = initialized
            elif args.command == "verify":
                result = service.verify(args.full)
            elif args.command == "list":
                result = service.catalog.list_assets()
            elif args.command == "recluster-bursts":
                result = service.recluster_bursts()
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
