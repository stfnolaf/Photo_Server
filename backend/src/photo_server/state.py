"""Mutation entry points and projection compatibility.

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
    """Publish the immutable revision, then apply the PostgreSQL projection."""
    previous = service.catalog.operation(operation_id)
    if previous is not None:
        if previous["request"] != mutation.document():
            from photo_server.config import LibraryError

            raise LibraryError("Operation ID was reused with a different request")
        return previous["result"]
    from photo_server.authoritative import AuthoritativeMutationCoordinator

    AuthoritativeMutationCoordinator(service).publish(operation_id, mutation)
    return service.catalog.commit_mutation(operation_id, mutation)


def mutate_face(service, operation_id, request: dict) -> dict:
    """Publish person/face durable state before applying the projection."""
    previous = service.catalog.operation(operation_id)
    if previous is not None:
        if previous["request"] != request:
            from photo_server.config import LibraryError

            raise LibraryError("Operation ID was reused with a different request")
        return previous["result"]
    from photo_server.authoritative import AuthoritativeMutationCoordinator

    AuthoritativeMutationCoordinator(service).publish_face_operation(operation_id, request)
    return service.catalog.commit_face_operation(operation_id, request)
