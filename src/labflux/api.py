"""Authenticated public and worker HTTP interfaces."""

from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse

from labflux import __version__, relay
from labflux.artifacts import ArtifactStore
from labflux.config import Settings
from labflux.hub import Hub
from labflux.models import Heartbeat, JobSpec, LogUpload, WorkerEvent, WorkerRegistration
from labflux.scheduler import Scheduler, ServiceError
from labflux.session_protocol import SessionRequest

DASHBOARD_PAGE = Path(__file__).parent / "dashboard.html"


def create_app(
    settings: Settings | None = None, *, run_loop: bool = True, clock=time.time
) -> FastAPI:
    settings = settings or Settings()
    service = Scheduler(settings, clock)
    hub = Hub()
    service.doorbell = hub.ring

    @asynccontextmanager
    async def lifespan(app):
        hub.bind(asyncio.get_running_loop())

        async def scheduling_loop():
            while True:
                await asyncio.to_thread(service.tick)
                await asyncio.sleep(settings.scheduler_interval_s)

        task = asyncio.create_task(scheduling_loop()) if run_loop else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            hub.bind(None)
            service.db.close()

    app = FastAPI(title="LabFlux", version=__version__, lifespan=lifespan)
    app.state.scheduler = service
    app.state.hub = hub
    app.state.settings = settings
    store = ArtifactStore(service)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    def bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise ServiceError(401, "Bearer token required")
        return authorization[7:]

    def identify(token: str) -> str | None:
        for identity, expected in settings.users.items():
            if hmac.compare_digest(token, expected):
                return identity
        return None

    def user(authorization: str | None = Header(default=None)) -> str:
        identity = identify(bearer(authorization))
        if identity is None:
            raise ServiceError(401, "invalid user token")
        return identity

    def worker(authorization: str | None = Header(default=None)) -> None:
        if not hmac.compare_digest(bearer(authorization), settings.worker_token):
            raise ServiceError(401, "invalid worker token")

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(DASHBOARD_PAGE, media_type="text/html")

    @app.post("/api/jobs", status_code=201)
    def submit(spec: JobSpec, owner: str = Depends(user)):
        return service.submit(owner, spec)

    @app.get("/api/jobs")
    def list_jobs(owner: str = Depends(user)):
        return service.list_jobs(owner)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, owner: str = Depends(user)):
        return service.get_job(job_id, owner)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, owner: str = Depends(user)):
        return service.cancel(job_id, owner)

    @app.get("/api/jobs/{job_id}/logs")
    def get_logs(
        job_id: str,
        after: int = Query(-1, ge=-1),
        attempt_id: str | None = None,
        owner: str = Depends(user),
    ):
        return service.get_logs(job_id, owner, after, attempt_id)

    @app.post("/api/sessions")
    def open_session(request: SessionRequest, owner: str = Depends(user)):
        return service.open_session(owner, request)

    @app.get("/api/sessions/current")
    def current_session(owner: str = Depends(user)):
        return service.current_session(owner)

    @app.post("/api/sessions/current/close")
    def close_session(owner: str = Depends(user)):
        return service.close_session(owner)

    @app.websocket("/api/sessions/{session_id}/ws")
    async def session_socket(websocket: WebSocket, session_id: str):
        """One fast-lane command (T6); the token is accepted only in the header."""
        header = websocket.headers.get("authorization", "")
        owner = identify(header[7:]) if header.startswith("Bearer ") else None
        if owner is None:
            await websocket.close(code=1008, reason="invalid user token")
            return
        await relay.serve(websocket, owner, session_id, service, hub)

    @app.get("/api/nodes")
    def list_nodes(owner: str = Depends(user)):
        return service.list_nodes()

    @app.post("/api/artifacts/sources", status_code=201)
    async def upload_source(request: Request, owner: str = Depends(user)):
        upload = await store.receive(request, settings.max_source_bytes)
        return store.store_source(owner, upload)

    @app.get("/api/artifacts/{artifact_id}")
    def download_artifact(artifact_id: str, owner: str = Depends(user)):
        path, value = store.user_download(artifact_id, owner)
        return FileResponse(
            path,
            media_type="application/gzip",
            filename=path.name,
            headers={"X-Content-SHA256": value["sha256"]},
        )

    @app.get(
        "/internal/workers/{node_id}/attempts/{attempt_id}/source", dependencies=[Depends(worker)]
    )
    def download_source(node_id: str, attempt_id: str, instance_id: str):
        path, value = store.worker_source(node_id, attempt_id, instance_id)
        return FileResponse(
            path, media_type="application/gzip", headers={"X-Content-SHA256": value["sha256"]}
        )

    @app.put(
        "/internal/workers/{node_id}/attempts/{attempt_id}/output", dependencies=[Depends(worker)]
    )
    async def upload_output(node_id: str, attempt_id: str, instance_id: str, request: Request):
        # Authorize before accepting a potentially large body.
        with service.db.transaction() as connection:
            service._attempt(connection, attempt_id, node_id, instance_id)
        upload = await store.receive(request, settings.max_output_bytes)
        return store.store_output(node_id, attempt_id, instance_id, upload)

    @app.websocket("/internal/workers/{node_id}/ws")
    async def worker_socket(websocket: WebSocket, node_id: str, instance_id: str = ""):
        """The doorbell connection (T4); also carries the fast-lane relay (T6)."""
        token = websocket.headers.get("authorization", "")
        if not token.startswith("Bearer ") or not hmac.compare_digest(
            token[7:], settings.worker_token
        ):
            await websocket.close(code=1008, reason="invalid worker token")
            return
        try:
            # A blocking DB transaction, so it must not run on the event loop.
            await asyncio.to_thread(service.check_instance, node_id, instance_id)
        except ServiceError as exc:
            await websocket.close(code=1008, reason=exc.detail)
            return
        await websocket.accept()
        await hub.serve_worker(node_id, instance_id, websocket)

    @app.post("/internal/workers/register", dependencies=[Depends(worker)])
    def register(payload: WorkerRegistration):
        return service.register(payload)

    @app.post("/internal/workers/{node_id}/heartbeat", dependencies=[Depends(worker)])
    def heartbeat(node_id: str, payload: Heartbeat):
        return service.heartbeat(node_id, payload)

    @app.get("/internal/workers/{node_id}/commands", dependencies=[Depends(worker)])
    def poll(node_id: str, instance_id: str):
        return service.poll_commands(node_id, instance_id)

    @app.post(
        "/internal/workers/{node_id}/commands/{command_id}/ack", dependencies=[Depends(worker)]
    )
    def ack(node_id: str, command_id: str, payload: Heartbeat):
        return service.ack(node_id, payload.instance_id, command_id)

    @app.post(
        "/internal/workers/{node_id}/attempts/{attempt_id}/events", dependencies=[Depends(worker)]
    )
    def event(node_id: str, attempt_id: str, payload: WorkerEvent):
        return service.handle_event(node_id, attempt_id, payload)

    @app.post(
        "/internal/workers/{node_id}/attempts/{attempt_id}/logs", dependencies=[Depends(worker)]
    )
    def upload_log(node_id: str, attempt_id: str, payload: LogUpload):
        return service.upload_log(node_id, attempt_id, payload)

    return app
