"""Command line entry point; commands are added alongside each milestone."""

import base64
import codecs
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import typer

from labflux import __version__
from labflux.archives import extract_archive, pack_directory
from labflux.config import load_settings
from labflux.logging import configure_logging
from labflux.models import JobSpec

app = typer.Typer(no_args_is_help=True, help="LabFlux resource manager and job scheduler")


@app.callback()
def main(ctx: typer.Context, config: Path | None = typer.Option(None, "--config", "-c")) -> None:
    """Read a TOML configuration, then run a command."""
    ctx.obj = load_settings(config)
    configure_logging()


@app.command()
def version() -> None:
    """Show the installed package version."""
    typer.echo(__version__)


@app.command()
def scheduler(ctx: typer.Context) -> None:
    """Start the single-process scheduler and HTTP API."""
    import uvicorn

    from labflux.api import create_app

    settings = ctx.obj
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,
        access_log=False,
    )


@app.command()
def worker(ctx: typer.Context) -> None:
    """Start a Docker worker; SIGINT/SIGTERM stop the worker but leave job containers running."""
    import signal

    from labflux.worker import Worker

    daemon = Worker(ctx.obj)
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: daemon.stop_event.set())
    try:
        daemon.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@app.command("db-upgrade")
def db_upgrade(ctx: typer.Context) -> None:
    """Apply supported SQLite schema migrations."""
    from labflux.database import Database

    database = Database(ctx.obj.data_dir)
    typer.echo(f"Schema version {database.version}: {database.path}")
    database.close()


@contextmanager
def api_client(settings):
    try:
        with httpx.Client(
            base_url=settings.scheduler_url,
            headers={"Authorization": f"Bearer {settings.user_token}"},
            timeout=30,
            trust_env=False,
        ) as client:
            yield client
    except httpx.HTTPError as exc:
        typer.echo(f"API request failed: {exc}", err=True)
        raise typer.Exit(1) from exc


def checked(response):
    response.raise_for_status()
    return response


def show(value):
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


@app.command()
def submit(
    ctx: typer.Context,
    spec: Path,
    source: Path | None = typer.Option(None, "--source"),
    wait: bool = typer.Option(False, "--wait"),
    wait_timeout: float = 120,
) -> None:
    """Submit a JSON job specification and optionally upload its source directory."""
    try:
        job = JobSpec.model_validate_json(spec.read_text())
    except (ValueError, OSError) as exc:
        typer.echo(f"Invalid job specification: {exc}", err=True)
        raise typer.Exit(1) from exc
    with api_client(ctx.obj) as client:
        if source:
            if not source.is_dir():
                raise typer.BadParameter("--source must be a directory")
            with tempfile.TemporaryDirectory(prefix="labflux-source-") as temp:
                archive = Path(temp) / "source.tar.gz"
                links = pack_directory(source, archive)
                if links:
                    typer.echo(
                        f"Symbolic links are not uploaded (links are not followed): "
                        f"{', '.join(links)}",
                        err=True,
                    )
                with archive.open("rb") as content:
                    uploaded = checked(
                        client.post(
                            "/api/artifacts/sources",
                            content=content,
                            headers={"Content-Type": "application/gzip"},
                        )
                    ).json()
                job.source_id = uploaded["id"]
        result = checked(client.post("/api/jobs", json=job.model_dump())).json()
        if wait:
            deadline = time.monotonic() + wait_timeout
            noted = False
            # UNKNOWN is not final: it clears when the worker reconnects or a restart adopts it.
            while result["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                if time.monotonic() >= deadline:
                    show(result)
                    typer.echo(
                        "Wait timed out; the job remains managed by the scheduler.", err=True
                    )
                    raise typer.Exit(1)
                if result["state"] == "UNKNOWN" and not noted:
                    typer.echo("The worker is unreachable (UNKNOWN); still waiting.", err=True)
                    noted = True
                time.sleep(0.25)
                result = checked(client.get(f"/api/jobs/{result['id']}")).json()
        show(result)
        if wait and result["state"] != "SUCCEEDED":
            raise typer.Exit(1)


@app.command()
def status(ctx: typer.Context, job_id: str | None = typer.Argument(None)) -> None:
    """List your jobs, or inspect one job including all attempts and outputs."""
    with api_client(ctx.obj) as client:
        show(checked(client.get(f"/api/jobs/{job_id}" if job_id else "/api/jobs")).json())


@app.command()
def nodes(ctx: typer.Context) -> None:
    """List worker capacity, allocations, observed load and heartbeat state."""
    with api_client(ctx.obj) as client:
        show(checked(client.get("/api/nodes")).json())


@app.command()
def cancel(ctx: typer.Context, job_id: str) -> None:
    """Cancel a queued job or request confirmed termination of a running job."""
    with api_client(ctx.obj) as client:
        show(checked(client.post(f"/api/jobs/{job_id}/cancel")).json())


@app.command()
def logs(
    ctx: typer.Context, job_id: str, follow: bool = False, attempt_id: str | None = None
) -> None:
    """Print stdout/stderr; --follow also retrieves output while the job is running."""
    after = -1
    decoders = {
        name: codecs.getincrementaldecoder("utf-8")("replace") for name in ("stdout", "stderr")
    }
    with api_client(ctx.obj) as client:
        while True:
            params = {"after": after}
            if attempt_id:
                params["attempt_id"] = attempt_id
            value = checked(client.get(f"/api/jobs/{job_id}/logs", params=params)).json()
            for chunk in value["chunks"]:
                text = decoders[chunk["stream"]].decode(base64.b64decode(chunk["data_base64"]))
                typer.echo(text, nl=False, err=chunk["stream"] == "stderr")
            after = value["next_seq"]
            if len(value["chunks"]) == 64:
                continue
            if not follow or value["terminal"]:
                break
            time.sleep(0.25)
        for name, decoder in decoders.items():
            typer.echo(decoder.decode(b"", final=True), nl=False, err=name == "stderr")


@app.command()
def fetch(
    ctx: typer.Context,
    job_id: str,
    directory: Path = Path("outputs"),
    attempt_id: str | None = None,
) -> None:
    """Download and safely extract one attempt's /outputs archive."""
    with api_client(ctx.obj) as client:
        job = checked(client.get(f"/api/jobs/{job_id}")).json()
        selected = attempt_id or job["current_attempt_id"]
        artifact = next((item for item in job["outputs"] if item["attempt_id"] == selected), None)
        if artifact is None:
            typer.echo("No output archive is available for that attempt.", err=True)
            raise typer.Exit(1)
        with tempfile.TemporaryDirectory(prefix="labflux-output-") as temp:
            archive = Path(temp) / "output.tar.gz"
            with client.stream("GET", f"/api/artifacts/{artifact['id']}") as response:
                checked(response)
                with archive.open("wb") as target:
                    for chunk in response.iter_bytes():
                        target.write(chunk)
            import hashlib

            with archive.open("rb") as content:
                if hashlib.file_digest(content, "sha256").hexdigest() != artifact["sha256"]:
                    raise typer.BadParameter("output checksum mismatch")
            extract_archive(
                archive,
                directory,
                max_bytes=ctx.obj.max_extracted_bytes,
                max_members=ctx.obj.max_archive_members,
            )
        typer.echo(str(directory.resolve()))


# Fast lane (M9). fastlane and websockets are imported lazily so a stand-in starts quickly.

PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}


def _project(cwd: Path):
    """(root, config) of the project above cwd, (None, None) without one; invalid exits 125."""
    from labflux import fastlane

    root = fastlane.find_project_root(cwd)
    if root is None:
        return None, None
    try:
        return root, fastlane.load_project(root)
    except fastlane.ProjectError as exc:
        typer.echo(f"labflux: {exc}", err=True)
        raise typer.Exit(fastlane.EXIT_ERROR) from exc


@app.command(context_settings=PASSTHROUGH)
def run(
    ctx: typer.Context,
    argv: list[str] = typer.Argument(..., help="The command, after --"),
    gpu: int | None = typer.Option(None, "--gpu"),
    cpu: float | None = typer.Option(None, "--cpu"),
    memory_mib: int | None = typer.Option(None, "--memory-mib"),
) -> None:
    """Run a command in your fast-lane session, leasing one when needed: run -- pytest -q."""
    from labflux import fastlane

    cwd = Path.cwd()
    root, project = _project(cwd)
    if root is None:
        typer.echo(
            f"labflux: no {fastlane.PROJECT_FILE} found above {cwd}; create one at the project "
            "root",
            err=True,
        )
        raise typer.Exit(fastlane.EXIT_ERROR)
    raise typer.Exit(
        fastlane.run_command(
            ctx.obj, root, project, argv, cwd, cpu=cpu, memory_mib=memory_mib, gpu=gpu
        )
    )


@app.command("_standin", hidden=True, context_settings=PASSTHROUGH)
def standin(ctx: typer.Context, name: str, args: list[str] | None = typer.Argument(None)) -> None:
    """Entry point of the stand-in scripts written by `labflux shell`."""
    from labflux import fastlane

    cwd = Path.cwd()
    try:
        decision = fastlane.route(name, args or [], cwd)
    except fastlane.ProjectError as exc:
        typer.echo(f"labflux: {exc}", err=True)
        raise typer.Exit(fastlane.EXIT_ERROR) from exc
    if isinstance(decision, fastlane.Local):
        raise typer.Exit(fastlane.exec_local(decision.argv))
    raise typer.Exit(
        fastlane.run_command(ctx.obj, decision.root, decision.project, decision.argv, cwd)
    )


@app.command(context_settings=PASSTHROUGH)
def local(argv: list[str] = typer.Argument(..., help="The command, after --")) -> None:
    """Run the real local command, bypassing the stand-ins: local -- python -V."""
    from labflux import fastlane

    raise typer.Exit(fastlane.exec_local(argv))


@app.command()
def shell(
    ctx: typer.Context, program: str | None = typer.Option(None, "--shell", help="Shell to run")
) -> None:
    """Start a shell whose PATH routes python, pytest, uv run, ... to your session.

    bash and zsh keep the stand-ins first on PATH after their start-up files and before every
    prompt; other shells only get PATH set once, before their start-up files run.
    """
    from labflux import fastlane

    _, project = _project(Path.cwd())
    config = ctx.find_root().params.get("config")  # The raw --config value, if given.
    config = Path(config) if config else None
    program = program or os.environ.get("SHELL") or "/bin/sh"
    raise typer.Exit(fastlane.run_shell(program, config, fastlane.standin_names(project)))


session_app = typer.Typer(no_args_is_help=True, help="Inspect or close your fast-lane session")
app.add_typer(session_app, name="session")


def _current_session(response):
    if response.status_code == 404:
        typer.echo("You have no fast-lane session.", err=True)
        raise typer.Exit(1)
    value = checked(response).json()
    if value.get("lease_expires_at") is not None:
        value["lease_remaining_s"] = max(0, round(value["lease_expires_at"] - time.time()))
    return value


@session_app.command("status")
def session_status(ctx: typer.Context) -> None:
    """Show your current session and the seconds left on its lease."""
    with api_client(ctx.obj) as client:
        show(_current_session(client.get("/api/sessions/current")))


@session_app.command("close")
def session_close(ctx: typer.Context) -> None:
    """Close your session; its container stops and its resources are released."""
    from labflux import fastlane

    with api_client(ctx.obj) as client:
        show(_current_session(client.post("/api/sessions/current/close")))
    root = fastlane.find_project_root(Path.cwd())
    if root is not None:
        fastlane.clear_remote_files(root)  # The session's files end with it.


@session_app.command("files")
def session_files() -> None:
    """List the files that exist only in the remote session (over the write-back limit)."""
    from labflux import fastlane

    cwd = Path.cwd()
    root = fastlane.find_project_root(cwd)
    if root is None:
        typer.echo(f"labflux: no {fastlane.PROJECT_FILE} found above {cwd}", err=True)
        raise typer.Exit(1)
    text = fastlane.read_remote_files(root)
    typer.echo(text or "No files exist only in the remote session.\n", nl=False)


if __name__ == "__main__":
    app()
