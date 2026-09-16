"""PostgreSQL-authoritative library mutations.

S3 contains immutable media blobs. Structured library state is committed in one
PostgreSQL transaction and is protected by PostgreSQL backups; it is not mirrored
into per-asset or per-album S3 documents.
"""

from photo_server.models import Album, Manifest, Mutation


def import_identity(manifest: Manifest) -> dict:
    """Return the immutable portion of an asset record."""
    return {
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
    """Commit a mutation and its idempotency record atomically in PostgreSQL."""
    return service.catalog.commit_mutation(operation_id, mutation)
