"""The photo server's client for the standalone face-service.

Phase 2B of ``docs/ai-service-split-plan.md``: face inference (YuNet
detection + AdaFace IR101 embedding) runs only in the face-service process;
this module is the thin HTTP client the AI worker calls instead of the
in-process analyzer that the same change removed from ``analysis.py``. The
photo server package never imports ``face_service`` — the same split as
``upload_client`` (the server keeps a thin client, the service keeps the
models).

The hosted-provider contract applies (Q1): ``Authorization: Bearer <token>``
when a token is set, OpenAI-shaped JSON errors, and ``429`` + ``Retry-After``
from the service's saturated queue. A 429 is a pacing signal, so it maps to
the service-unavailable class (Phase 3A requeues jobs on it), never to a job
failure.

The identity guard is the client side of the dual pin: every call first
verifies the service's reported embedding model (name/revision/
weightsSha256) against ``ADAFACE_IDENTITY`` — the byte-identical constant
the service verifies its own weights against at startup. A mismatch means
the service swapped models out from under a running library (stored
embeddings would not be comparable), so it is treated as unavailable —
detected live on every call, not just at startup (one embedding space,
global invariant).
"""

import math

import httpx

# The photo server's copy of the verified embedding-model identity. The
# face-service keeps its own byte-identical copy (face_service.analyzer);
# the two must never drift (dual pin).
ADAFACE_IDENTITY = {
    "name": "adaface-ir101",
    "revision": "54f602a0737bd1ee4a4e7e9fd089a485f397fefd",
    "weights_sha256": "2ea535a43877bd3de8091903935c783ce335be66a9f8917fae9a7a18ae4bbf56",
    "preprocessing": "RGB float32 [-1,1] NCHW, ArcFace 112x112",
}

# A health probe is a short liveness/identity check, not an inference request.
HEALTH_TIMEOUT_SECONDS = 5.0


class FaceServiceError(Exception):
    """The face-service rejected or mangled a request (401, other 4xx, 500,
    or an unusable response shape): the analysis job fails with it, exactly
    as an in-process detector error did before the split."""


class FaceServiceUnavailable(FaceServiceError):
    """The face-service is not usable right now: not configured, connection
    error, timeout, 429 pacing, 502-504, still starting, or an embedding-model
    identity mismatch. Phase 3A routes this class to job requeue instead of
    failure; until then it fails the job with the face stage, as today."""


def _error_detail(response: httpx.Response) -> str:
    """The service's OpenAI-shaped error message, or the raw body as a
    fallback (mirrors the VLM client in analysis.py)."""
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return str(body["error"].get("message") or "")
    except (httpx.DecodingError, ValueError):
        pass
    return response.text[:200]


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    message = f"face service request failed with HTTP {response.status_code}"
    detail = _error_detail(response)
    if detail:
        message = f"{message}: {detail}"
    if response.status_code == 429 or response.status_code in (502, 503, 504):
        # Pacing or upstream failure: the job is requeued, not failed
        # (3A); a saturated queue is exactly the case the service paces for.
        raise FaceServiceUnavailable(message) from None
    raise FaceServiceError(message) from None


def _check_identity(payload: dict) -> None:
    """Client side of the dual pin: the embedding model the service reports
    must match the verified identity recorded in this package."""
    models = payload.get("models")
    reported = models.get("faceEmbedding") if isinstance(models, dict) else None
    if not isinstance(reported, dict):
        raise FaceServiceUnavailable("face service health reports no faceEmbedding identity")
    for health_key, identity_key in (
        ("name", "name"),
        ("revision", "revision"),
        ("weightsSha256", "weights_sha256"),
    ):
        if reported.get(health_key) != ADAFACE_IDENTITY[identity_key]:
            raise FaceServiceUnavailable(
                f"face service embedding model mismatch: service reports "
                f"{health_key}={reported.get(health_key)!r}, expected "
                f"{ADAFACE_IDENTITY[identity_key]!r}"
            )


def _validate_faces(faces: object) -> list[dict]:
    """The face list in the exact shape the removed in-process analyzer
    returned: dicts of a normalized 4-tuple box, a confidence, and a
    non-empty finite embedding (the matcher and the S3 artifact consume
    those fields verbatim)."""
    if not isinstance(faces, list) or not all(isinstance(face, dict) for face in faces):
        raise FaceServiceError(f"face service response has an unusable faces shape: {faces!r}")
    for face in faces:
        box = face.get("box")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in box)
        ):
            raise FaceServiceError(f"face service returned an invalid box: {box!r}")
        confidence = face.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise FaceServiceError(f"face service returned an invalid confidence: {confidence!r}")
        embedding = face.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise FaceServiceError(f"face service returned an invalid embedding: {embedding!r}")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in embedding
        ):
            raise FaceServiceError(f"face service returned invalid embeddings: {embedding!r}")
    return faces


class RemoteFaceAnalyzer:
    """The worker's face stage: the face-service, over HTTP.

    ``analyze(jpeg)`` returns the face list in the exact shape the removed
    in-process analyzer returned (normalized ``box``, ``confidence``, 512-d
    ``embedding``); ``health()`` verifies the model identity and returns the
    ``/health`` payload; ``model_version`` is the verified runtime label the
    analysis artifact records under ``models.faceEmbedding.runtime``. The
    analyzer holds the settings it was constructed with; the worker builds
    it once from the service's settings.
    """

    def __init__(self, settings):
        self._settings = settings
        self._model_version = ""

    @property
    def model_version(self) -> str:
        """The embedding model's runtime label, from the last verified health."""
        return self._model_version

    def _client(self, timeout: float) -> httpx.Client:
        settings = self._settings
        if not settings.face_service_url:
            raise FaceServiceUnavailable(
                "PHOTO_FACE_SERVICE_URL is not set; the face service is unconfigured"
            )
        headers = (
            {"Authorization": f"Bearer {settings.face_service_token}"}
            if settings.face_service_token
            else {}
        )
        return httpx.Client(
            base_url=settings.face_service_url.rstrip("/"),
            timeout=timeout,
            headers=headers,
        )

    def health(self, timeout: float = HEALTH_TIMEOUT_SECONDS) -> dict:
        """Probe the service and verify the embedding-model identity.

        Returns the ``/health`` payload (provenance plus live queue state).
        Raises ``FaceServiceUnavailable`` when the service is unreachable,
        timing out, pacing (429), 502-504, still starting (503), or reporting
        an embedding model that does not match ``ADAFACE_IDENTITY``;
        ``FaceServiceError`` on 401 or other 4xx and unusable shapes.

        ``timeout`` defaults to the worker's 5 s liveness budget; Phase 3B's
        API ``/health`` probe passes its shorter 3 s budget, reusing this
        exact probe and identity check (a drift therefore shows as
        unreachable there too).
        """
        with self._client(timeout) as client:
            try:
                response = client.get("/health")
            except httpx.TransportError as error:
                raise FaceServiceUnavailable(f"face service unreachable: {error}") from error
            _raise_for_status(response)
            try:
                payload = response.json()
            except (httpx.DecodingError, ValueError) as error:
                raise FaceServiceError(f"face service health is not JSON: {error}") from error
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise FaceServiceError(f"face service health has an unusable shape: {payload!r}")
        _check_identity(payload)
        self._model_version = str(payload["models"]["faceEmbedding"]["runtime"])
        return payload

    def analyze(self, jpeg: bytes) -> list[dict]:
        """Run the face stage: verify the service, then send the raw JPEG.

        The JPEG is the worker's ``prepare_jpeg`` output (bounded,
        metadata-free): the service needs no resize policy of its own.
        """
        # Identity check on every call: a live model swap is detected per
        # image, not just at startup (one embedding space).
        self.health()
        with self._client(self._settings.face_service_timeout) as client:
            try:
                response = client.post(
                    "/v1/faces/analyze",
                    content=jpeg,
                    headers={"Content-Type": "image/jpeg"},
                )
            except httpx.TransportError as error:
                raise FaceServiceUnavailable(f"face service unreachable: {error}") from error
            _raise_for_status(response)
            try:
                payload = response.json()
            except (httpx.DecodingError, ValueError) as error:
                raise FaceServiceError(f"face service response is not JSON: {error}") from error
        if not isinstance(payload, dict):
            raise FaceServiceError(f"face service response has an unusable shape: {payload!r}")
        return _validate_faces(payload.get("faces"))
