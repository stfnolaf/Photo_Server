"""S3-authoritative mutation entry points and database projection updates."""

from threading import RLock
from uuid import UUID

from photo_server.models import Album, Manifest, Mutation

# S3 revision publication and its PostgreSQL projection must be serialized in
# this process.  PostgreSQL's writer lock protects the projection transaction,
# but cannot span the preceding immutable S3 write because that write uses a
# separate storage client.  This lock closes that intra-process revision race.
_MUTATION_LOCK = RLock()


def import_identity(manifest: Manifest) -> dict:
    """Return the immutable portion of an asset record."""
    identity = {
        key: value
        for key, value in manifest.document().items()
        if key
        in {
            "libraryId",
            "assetId",
            "primaryBlobId",
            "blobs",
            "importedAt",
        }
    }
    imported_at = identity["importedAt"]
    if imported_at.endswith("+00:00"):
        identity["importedAt"] = imported_at[:-6] + "Z"
    return identity


def mutation_result(snapshot: Manifest | Album) -> dict:
    if isinstance(snapshot, Album):
        return snapshot.document()
    return {
        **snapshot.user_state.document(),
        "assetId": str(snapshot.asset_id),
        "operationId": str(snapshot.operation_id),
        "revision": snapshot.revision,
        "deletedAt": snapshot.deleted_at,
    }


def mutate(service, operation_id, mutation: Mutation) -> dict:
    """Publish the immutable revision, then apply the PostgreSQL projection."""
    previous = service.catalog.operation(operation_id)
    if previous is not None:
        if previous["request"] != mutation.document():
            from photo_server.config import LibraryError

            raise LibraryError("Operation ID was reused with a different request")
        return previous["result"]
    kind, action = mutation.action.split(".")
    if kind == "burst":
        from photo_server.burst_authority import BurstAuthority

        asset_id = mutation.changes.get(
            "representativeAssetId" if action == "setRepresentative" else "assetId"
        )
        try:
            result = BurstAuthority(service).mutate(
                operation_id, action, str(mutation.entity_id), str(asset_id)
            )
            return service.catalog.record_burst_operation(operation_id, mutation, result)
        except Exception as error:
            service.publisher.record_reconciliation(
                operation_id,
                {
                    "status": "canonical-written-projection-failed",
                    "entityId": str(mutation.entity_id),
                    "action": mutation.action,
                    "error": str(error),
                },
            )
            raise
    from photo_server.authoritative import AuthoritativeMutationCoordinator

    with _MUTATION_LOCK:
        snapshot = AuthoritativeMutationCoordinator(service).publish(operation_id, mutation)
        if mutation.action in {"asset.delete", "asset.restore"}:
            from photo_server.burst_authority import BurstAuthority

            BurstAuthority(service).synchronize(operation_id, str(mutation.entity_id))
        try:
            return service.catalog.commit_mutation(operation_id, mutation, snapshot)
        except Exception as error:
            service.publisher.record_reconciliation(
                operation_id,
                {
                    "status": "canonical-written-projection-failed",
                    "entityId": str(mutation.entity_id),
                    "action": mutation.action,
                    "error": str(error),
                },
            )
            raise


def mutate_face(service, operation_id, request: dict) -> dict:
    """Publish person/face durable state before applying the projection."""
    previous = service.catalog.operation(operation_id)
    if previous is not None:
        if previous["request"] != request:
            from photo_server.config import LibraryError

            raise LibraryError("Operation ID was reused with a different request")
        return previous["result"]
    from photo_server.authoritative import AuthoritativeMutationCoordinator

    with _MUTATION_LOCK:
        AuthoritativeMutationCoordinator(service).publish_face_operation(operation_id, request)
        try:
            return service.catalog.commit_face_operation(operation_id, request)
        except Exception as error:
            service.publisher.record_reconciliation(
                operation_id,
                {"status": "canonical-written-projection-failed", "faceOperation": request, "error": str(error)},
            )
            raise


def mutate_ai(service, request: dict) -> dict:
    """Publish an AI run S3-first, then apply its projection.

    The run id is derived from the result content, so it doubles as
    the operation id.  The precheck compares only the stable identity
    fields: the stored request is enriched with the first publication's
    face assignments, which are byte-stable for the same content but
    not part of the logical identity.
    """
    from photo_server.ai_publication import derive_run_id, result_sha

    run_id = str(
        derive_run_id(
            service.library_id,
            request["result"]["analysisType"],
            request["assetId"],
            request["result"]["inputSha256"],
            request["result"]["pipelineVersion"],
            result_sha(request["result"]),
        )
    )
    previous = service.catalog.operation(run_id)
    if previous is not None:
        stable = {
            key: value
            for key, value in previous["request"].items()
            if key in {"assetId", "result", "sourceObjectKey", "reusePolicyVersion"}
        }
        current = {key: request.get(key) for key in stable}
        if stable != current:
            from photo_server.config import LibraryError

            raise LibraryError("Operation ID was reused with a different request")
        return previous["result"]
    from photo_server.authoritative import AuthoritativeMutationCoordinator

    with _MUTATION_LOCK:
        final = AuthoritativeMutationCoordinator(service).publish_ai_analysis(
            UUID(run_id), request
        )
        try:
            return service.catalog.commit_ai_analysis(run_id, final)
        except Exception as error:
            service.publisher.record_reconciliation(
                run_id,
                {
                    "status": "canonical-written-projection-failed",
                    "runId": run_id,
                    "error": str(error),
                },
            )
            raise
