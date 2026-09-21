"""The face-service HTTP app: a stateless, token-authenticated inference endpoint.

This module is the service process only; the photo server never imports it
(the split mirrors ``photo_server.upload_client`` — the server keeps a thin
client, the service keeps the models). The hosted-provider contract applies:

- ``Authorization: Bearer <token>`` on every endpoint (401 otherwise; an
  empty token logs a startup warning and is dev-only).
- OpenAI-shaped JSON errors: ``{"error": {"message": ...}}``.
- ``429`` + ``Retry-After`` when the internal FIFO queue saturates, the same
  pacing semantics as OpenAI/Anthropic (the client treats them as
  unavailable-and-requeue, never as a job failure).
- TLS for cross-machine use: a self-signed CA + server certificate
  auto-generated on first start (persisted in ``PHOTO_FACE_TLS_DIR``, e.g. the
  ``face-tls`` volume). Plain HTTP stays acceptable docker-internal.

Endpoints:

- ``GET /health`` — the verified model provenance plus live queue state.
  ``503 {"status": "starting"}`` until the analyzer session is built, and a
  failed build exits the process non-zero (a process that cannot embed is not
  a face service — there is no silent CPU fallback).
- ``POST /v1/faces/analyze`` — raw JPEG in, normalized boxes and 512-d
  unit-normalized AdaFace embeddings out, exactly the shape the photo
  server's in-process analyzer returned before the split.
"""

import argparse
import asyncio
import hmac
import io
import logging
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image

from face_service.analyzer import ADAFACE_IDENTITY, AdaFaceAnalyzer
from face_service.config import Settings

log = logging.getLogger("face_service")

# Soft queue cap: a wedged client must not pile up memory in the service.
# Beyond the bound the service paces the client instead of queueing
# indefinitely (the 429 path is how the photo server's dispatcher absorbs it).
QUEUE_CAP = 64
RETRY_AFTER_SECONDS = 2
MAX_BODY_BYTES = 16 * 1024 * 1024


@dataclass
class _Job:
    jpeg: bytes
    future: asyncio.Future


class _Executor:
    """A FIFO queue of analyze jobs in front of ``concurrency`` in-flight slots.

    Each slot is an asyncio worker task that pulls jobs off the shared queue,
    so jobs run in arrival order up to the bound. Slot 0's analyzer is built
    at startup (its failure is fatal); further slots build their own verified
    session on first use, so the stateful OpenCV detector state is never
    shared across threads.
    """

    def __init__(self, analyzer_factory, concurrency: int, queue_cap: int = QUEUE_CAP):
        self.analyzer_factory = analyzer_factory
        self.concurrency = concurrency
        self.in_flight = 0
        self.queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_cap)
        self.analyzers: list = []
        self._workers: list[asyncio.Task] = []

    def start(self) -> None:
        for slot in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker(slot)))

    def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()

    def try_enqueue(self, job: _Job) -> bool:
        try:
            self.queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            return False

    def _analyzer_for(self, slot: int):
        while len(self.analyzers) <= slot:
            self.analyzers.append(self.analyzer_factory())
        return self.analyzers[slot]

    async def _worker(self, slot: int) -> None:
        while True:
            job = await self.queue.get()
            self.in_flight += 1
            try:
                analyzer = self._analyzer_for(slot)
                try:
                    payload = await anyio.to_thread.run_sync(analyzer.analyze, job.jpeg)
                except Exception as error:
                    if not job.future.done():
                        job.future.set_exception(error)
                else:
                    if not job.future.done():
                        job.future.set_result(payload)
            finally:
                self.in_flight -= 1
                self.queue.task_done()


def _authorized(request: Request, settings: Settings) -> bool:
    if not settings.face_service_token:
        return True
    provided = request.headers.get("authorization", "")
    expected = f"Bearer {settings.face_service_token}"
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _error(status_code: int, message: str, headers: dict | None = None) -> JSONResponse:
    """An OpenAI-shaped error body: ``{"error": {"message": ...}}``."""
    return JSONResponse({"error": {"message": message}}, status_code=status_code, headers=headers)


def _runtime_label(analyzer) -> str:
    """The onnxruntime build the session actually runs on (provenance)."""
    label = getattr(analyzer, "runtime_label", None)
    if isinstance(label, str) and label:
        return label
    try:
        import onnxruntime
    except ImportError:
        return "unknown"
    return f"onnxruntime-{onnxruntime.__version__}"


async def _read_bounded(request: Request, cap: int) -> bytes | None:
    """Stream the body up to ``cap`` bytes; ``None`` when it exceeds the cap."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _write_pem(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def ensure_tls(directory: Path) -> tuple[Path, Path]:
    """Load or auto-generate the self-signed CA and server certificate.

    The CA is created once and persisted in ``directory`` (mount a volume
    there, e.g. ``face-tls``, so restarts keep using it); the server
    certificate is signed by that CA and names ``localhost``, ``face-service``
    (the compose service name), and this machine's hostname. Cross-machine
    callers pin the CA on the photo server side (``PHOTO_FACE_SERVICE_CA``);
    alternatively a reverse proxy terminates TLS with a real certificate.
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key = directory / "ca.crt", directory / "ca.key"
    server_crt, server_key = directory / "server.crt", directory / "server.key"
    now = datetime.datetime.now(datetime.timezone.utc)

    if ca_crt.is_file() and ca_key.is_file():
        ca_key_obj = serialization.load_pem_private_key(ca_key.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_crt.read_bytes())
    else:
        ca_key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "photo face-service CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(ca_key_obj.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(ca_key_obj, hashes.SHA256())
        )
        _write_pem(ca_crt, ca_cert.public_bytes(serialization.Encoding.PEM))
        _write_pem(
            ca_key,
            ca_key_obj.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ),
        )
        log.info("Generated new face-service CA at %s", ca_crt)

    if not (server_crt.is_file() and server_key.is_file()):
        server_key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "face-service")])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(server_key_obj.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.DNSName("face-service"),
                        x509.DNSName(socket.gethostname()),
                    ]
                ),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        )
        server_cert = builder.sign(ca_key_obj, hashes.SHA256())
        _write_pem(server_crt, server_cert.public_bytes(serialization.Encoding.PEM))
        _write_pem(
            server_key,
            server_key_obj.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ),
        )
        log.info("Generated new face-service certificate at %s", server_crt)

    return server_crt, server_key


def create_app(settings: Settings, *, queue_cap: int = QUEUE_CAP) -> FastAPI:
    """Build the FastAPI app (also the seam tests monkeypatch around)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.face_service_token:
            log.warning(
                "PHOTO_FACE_SERVICE_TOKEN is empty: the service is unauthenticated. "
                "Set it for anything beyond docker-internal / dev use."
            )
        app.state.executor.start()
        # The server accepts requests (and answers /health) before the
        # session is built: startup reports 503 "starting" in the meantime.
        asyncio.create_task(_startup(app))
        yield
        app.state.executor.stop()

    async def _startup(app: FastAPI) -> None:
        try:
            analyzer = await anyio.to_thread.run_sync(lambda: AdaFaceAnalyzer(settings))
        except Exception as error:
            log.error("face-service startup failed: %s", error)
            os._exit(1)
        app.state.analyzer = analyzer
        app.state.executor.analyzers = [analyzer]
        app.state.ready = True
        log.info(
            "face-service ready: %s (concurrency %d, threshold %s)",
            ADAFACE_IDENTITY["name"],
            settings.face_service_concurrency,
            settings.face_detection_threshold,
        )

    app = FastAPI(title="face-service", lifespan=lifespan)
    app.state.settings = settings
    app.state.ready = False
    app.state.analyzer = None
    app.state.executor = _Executor(
        lambda: AdaFaceAnalyzer(settings), settings.face_service_concurrency, queue_cap
    )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        if not app.state.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        if not _authorized(request, settings):
            return _error(401, "Invalid or missing bearer token")
        return JSONResponse(
            {
                "status": "ok",
                "models": {
                    "faceDetector": "yunet-2023mar",
                    "faceEmbedding": {
                        "name": ADAFACE_IDENTITY["name"],
                        "revision": ADAFACE_IDENTITY["revision"],
                        "weightsSha256": ADAFACE_IDENTITY["weights_sha256"],
                        "runtime": _runtime_label(app.state.analyzer),
                    },
                },
                "detectionThreshold": settings.face_detection_threshold,
                "concurrency": settings.face_service_concurrency,
                "inFlight": app.state.executor.in_flight,
                "queueDepth": app.state.executor.queue.qsize(),
            }
        )

    @app.post("/v1/faces/analyze")
    async def analyze(request: Request) -> JSONResponse:
        if not app.state.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        if not _authorized(request, settings):
            return _error(401, "Invalid or missing bearer token")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "image/jpeg":
            return _error(415, "Content-Type must be image/jpeg")
        body = await _read_bounded(request, MAX_BODY_BYTES)
        if body is None:
            return _error(413, f"Request body exceeds {MAX_BODY_BYTES // (1024 * 1024)} MB")
        try:
            with Image.open(io.BytesIO(body)) as image:
                image.verify()
        except Exception:
            return _error(422, "Request body is not a decodable JPEG")
        job = _Job(body, asyncio.get_running_loop().create_future())
        if not app.state.executor.try_enqueue(job):
            return _error(
                429,
                "Face service queue is saturated",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        try:
            faces = await job.future
        except Exception as error:
            return _error(500, f"Face analysis failed: {error}")
        return JSONResponse({"schemaVersion": 1, "faces": faces})

    return app


def serve(settings: Settings | None = None) -> None:
    """Run the service under uvicorn (the console-script entry point)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or Settings()
    host, _, port = settings.face_service_bind.rpartition(":")
    host = host or "0.0.0.0"
    port = int(port or 8901)
    kwargs: dict = {}
    if settings.face_tls_dir is not None:
        cert, key = ensure_tls(settings.face_tls_dir)
        kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    uvicorn.run(create_app(settings), host=host, port=port, **kwargs)


def main(argv: list[str] | None = None) -> None:
    """Console-script entry: ``face-service serve``."""
    parser = argparse.ArgumentParser(
        prog="face-service",
        description="Stateless face detection and AdaFace embedding service.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP service under uvicorn")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve()


if __name__ == "__main__":
    main()
