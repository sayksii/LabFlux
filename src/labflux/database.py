"""SQLite persistence and serialized allocation transactions."""

from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    select,
)

metadata = MetaData()
schema_version = Table("schema_version", metadata, Column("version", Integer, primary_key=True))
nodes = Table(
    "nodes",
    metadata,
    Column("id", String, primary_key=True),
    Column("instance_id", String, nullable=False),
    Column("capacity", JSON, nullable=False),
    Column("observed", JSON, nullable=False),
    Column("status", String, nullable=False),
    Column("last_seen", Float, nullable=False),
)
jobs = Table(
    "jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("submission_seq", Integer, nullable=False, unique=True),
    Column("owner", String, nullable=False, index=True),
    Column("spec", JSON, nullable=False),
    Column("state", String, nullable=False, index=True),
    Column("reason", Text, nullable=False),
    Column("current_attempt_id", String),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
attempts = Table(
    "attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("job_id", String, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("node_id", String, ForeignKey("nodes.id"), nullable=False, index=True),
    Column("state", String, nullable=False),
    Column("resources", JSON, nullable=False),
    Column("released", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("exit_code", Integer),
    Column("reason", Text, nullable=False),
    Column("oom_killed", Boolean, nullable=False, default=False),
)
commands = Table(
    "commands",
    metadata,
    Column("id", String, primary_key=True),
    Column("node_id", String, ForeignKey("nodes.id"), nullable=False, index=True),
    Column("attempt_id", String, ForeignKey("attempts.id"), nullable=False),
    Column("kind", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("acked", Boolean, nullable=False, default=False),
    Column("created_at", Float, nullable=False),
)
events = Table(
    "events",
    metadata,
    Column("id", String, primary_key=True),
    Column("attempt_id", String, ForeignKey("attempts.id"), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
logs = Table(
    "logs",
    metadata,
    Column("attempt_id", String, ForeignKey("attempts.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("stream", String, nullable=False),
    Column("data", LargeBinary, nullable=False),
)
artifacts = Table(
    "artifacts",
    metadata,
    Column("id", String, primary_key=True),
    Column("owner", String, nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("sha256", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("relative_path", String, nullable=False),
    Column("attempt_id", String, ForeignKey("attempts.id")),
    Column("created_at", Float, nullable=False),
)


class Database:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "scheduler.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"check_same_thread": False, "timeout": 30}
        )
        self.lock = RLock()

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")

        self.upgrade()

    def upgrade(self) -> None:
        """Migration 1 bootstraps the schema. Later changes must add a numbered migration."""
        with self.engine.begin() as connection:
            schema_version.create(connection, checkfirst=True)
            current = connection.execute(select(schema_version.c.version)).scalar_one_or_none()
            if current is None:
                metadata.create_all(connection)
                connection.execute(schema_version.insert().values(version=1))
            elif current != 1:
                raise RuntimeError(f"unsupported schema version: {current}")

    @contextmanager
    def transaction(self):
        # BEGIN IMMEDIATE protects read -> reserve -> dispatch, including concurrent API threads.
        with self.lock, self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def close(self) -> None:
        self.engine.dispose()
