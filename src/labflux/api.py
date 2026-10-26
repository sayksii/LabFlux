"""Authenticated public and worker HTTP interfaces."""

from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from labflux import __version__
from labflux.artifacts import ArtifactStore
from labflux.config import Settings
from labflux.models import Heartbeat, JobSpec, LogUpload, WorkerEvent, WorkerRegistration
from labflux.scheduler import Scheduler, ServiceError


def create_app(
    settings: Settings | None = None, *, run_loop: bool = True, clock=time.time
) -> FastAPI:
    settings = settings or Settings()
    service = Scheduler(settings, clock)

    @asynccontextmanager
    async def lifespan(app):
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
            service.db.close()

    app = FastAPI(title="LabFlux", version=__version__, lifespan=lifespan)
    app.state.scheduler = service
    app.state.settings = settings
    store = ArtifactStore(service)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    def bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise ServiceError(401, "Bearer token required")
        return authorization[7:]

    def user(authorization: str | None = Header(default=None)) -> str:
        token = bearer(authorization)
        for identity, expected in settings.users.items():
            if hmac.compare_digest(token, expected):
                return identity
        raise ServiceError(401, "invalid user token")

    def worker(authorization: str | None = Header(default=None)) -> None:
        if not hmac.compare_digest(bearer(authorization), settings.worker_token):
            raise ServiceError(401, "invalid worker token")

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

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
