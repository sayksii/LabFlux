"""Authenticated metadata and filesystem storage for sources and results."""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select

from labflux.database import artifacts
from labflux.scheduler import Scheduler, ServiceError, row_dict


class ArtifactStore:
    def __init__(self, service: Scheduler):
        self.service = service
        self.root = service.settings.data_dir / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    async def receive(self, request, limit: int) -> tuple[Path, str, int]:
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=self.root, prefix="incoming-", delete=False) as target:
            path = Path(target.name)
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise ServiceError(413, "artifact exceeds configured upload limit")
                    target.write(chunk)
                    digest.update(chunk)
                if size == 0:
                    raise ServiceError(422, "empty upload")
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return path, digest.hexdigest(), size

    def store_source(self, owner: str, upload: tuple[Path, str, int]) -> dict:
        path, digest, size = upload
        try:
            with self.service.db.transaction() as connection:
                existing = connection.execute(
                    select(artifacts).where(
                        artifacts.c.owner == owner,
                        artifacts.c.kind == "SOURCE",
                        artifacts.c.sha256 == digest,
                    )
                ).first()
                if existing:
                    return self.service._artifact_view(row_dict(existing))
                return self._insert(connection, owner, "SOURCE", path, digest, size)
        finally:
            path.unlink(missing_ok=True)

    def store_output(
        self, node_id: str, attempt_id: str, instance_id: str, upload: tuple[Path, str, int]
    ) -> dict:
        path, digest, size = upload
        try:
            with self.service.db.transaction() as connection:
                attempt = self.service._attempt(connection, attempt_id, node_id, instance_id)
                owner = self.service._job(connection, attempt["job_id"])["owner"]
                existing = connection.execute(
                    select(artifacts).where(
                        artifacts.c.attempt_id == attempt_id, artifacts.c.kind == "OUTPUT"
                    )
                ).first()
                if existing:
                    if existing.sha256 != digest:
                        raise ServiceError(409, "an output with different contents already exists")
                    return self.service._artifact_view(row_dict(existing))
                return self._insert(connection, owner, "OUTPUT", path, digest, size, attempt_id)
        finally:
            path.unlink(missing_ok=True)

    def _insert(self, connection, owner, kind, incoming, digest, size, attempt_id=None) -> dict:
        artifact_id = uuid.uuid4().hex
        final = self.root / f"{artifact_id}.tar.gz"
        incoming.replace(final)
        value = {
            "id": artifact_id,
            "owner": owner,
            "kind": kind,
            "sha256": digest,
            "size": size,
            "relative_path": final.name,
            "attempt_id": attempt_id,
            "created_at": self.service.clock(),
        }
        try:
            connection.execute(artifacts.insert().values(**value))
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        return self.service._artifact_view(dict(value))

    def user_download(self, artifact_id: str, owner: str) -> tuple[Path, dict]:
        with self.service.db.transaction() as connection:
            row = connection.execute(
                select(artifacts).where(artifacts.c.id == artifact_id, artifacts.c.owner == owner)
            ).first()
            if row is None:
                raise ServiceError(404, "artifact not found")
            value = row_dict(row)
        return self.root / value["relative_path"], value

    def worker_source(self, node_id: str, attempt_id: str, instance_id: str) -> tuple[Path, dict]:
        with self.service.db.transaction() as connection:
            attempt = self.service._attempt(connection, attempt_id, node_id, instance_id)
            job = self.service._job(connection, attempt["job_id"])
            source_id = job["spec"]["source_id"]
            if not source_id:
                raise ServiceError(404, "job has no source artifact")
        return self.user_download(source_id, job["owner"])
