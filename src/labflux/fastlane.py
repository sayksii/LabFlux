"""Fast-lane client (M9): project discovery, command routing (T7), stand-ins and `labflux run`.

The CLI leases the user's session over HTTP, then sends each command over its own WebSocket:
an ExecRequest, the project snapshot (D4b), and optionally a cancel. It prints the relayed
output as raw bytes, applies the write-back archive (D12) and exits with the remote exit code,
or 124 (timeout), 130 (cancelled), 125 (LabFlux error) and 127 (stand-in command not found).
Ctrl-C stops the command only; the session stays for the next one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from labflux.archives import extract_archive, pack_directory
from labflux.models import ResourceRequest
from labflux.session_protocol import (
    DEFAULT_ROUTED_COMMANDS,
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    PROJECT_FILE,
    REMOTE_SUFFIX,
    SESSION_READY_STATE,
    STANDIN_MARKER,
    UV_COMMAND,
    UV_ROUTED_SUBCOMMAND,
    WORKSPACE,
    CancelRequest,
    CommandResult,
    ErrorMessage,
    ExecRequest,
    Notice,
    Output,
    SessionRequest,
    SessionView,
    SkippedFile,
    SnapshotChunk,
    WritebackChunk,
    decode,
    exit_status,
    iter_chunks,
    parse_server_message,
    writeback_excluded,
)

POLL_S = 0.25  # Waiting for a queued or starting session.
RECV_S = 0.2  # Granularity for noticing a signal while waiting for relayed messages.
WORKER_RETRY_S = 10.0  # How long worker_unavailable is retried, once a second.
BUSY_RETRY_S = 1.0  # Between attempts while the session runs another command.
DEFAULT_BUSY_WAIT_S = 120.0  # Total wait for a busy session; [session] busy_wait_s overrides.
STATE_DIR = ".labflux"  # Client state inside the project; never snapshotted or written back.
REMOTE_FILES = f"{STATE_DIR}/remote-files.txt"  # The session's remote-only files (see below).
HINT_NAMES = 3  # File names shown in the one-line hint.
ENDED_STATES = {"STOPPING", "SUCCEEDED", "FAILED", "CANCELLED"}
EXIT_BROKEN_PIPE = 128 + signal.SIGPIPE
# File timestamps come from a coarse kernel clock (one jiffy, up to 10 ms), so a local edit just
# after the command starts can carry an earlier mtime. Starting the conflict window this much
# earlier errs toward a harmless extra .remote copy instead of overwriting a local edit.
MTIME_SLACK_NS = 50_000_000


# Project configuration -----------------------------------------------------------------------


class SessionDefaults(BaseModel):
    """[session] in .labflux.toml; command-line flags take precedence."""

    model_config = ConfigDict(extra="forbid")
    cpu: float | None = None
    memory_mib: int | None = None
    gpu: int | None = None
    gpu_model: str | None = None
    min_vram_mib: int | None = None
    image: str | None = Field(default=None, min_length=1, max_length=512)
    datasets: list[str] = Field(default_factory=list)
    # A session runs one command at a time: how long a command waits for the one before it.
    busy_wait_s: float = Field(default=DEFAULT_BUSY_WAIT_S, ge=0)


class RouteConfig(BaseModel):
    """[route] in .labflux.toml: commands added to or removed from the remote list (T7)."""

    model_config = ConfigDict(extra="forbid")
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)

    @field_validator("add", "remove")
    @classmethod
    def bare_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", value) or value in {".", ".."}:
                raise ValueError(f"route entries must be bare command names: {value!r}")
        return values


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: SessionDefaults = Field(default_factory=SessionDefaults)
    route: RouteConfig = Field(default_factory=RouteConfig)


class ProjectError(ValueError):
    """An unreadable or invalid .labflux.toml."""


def find_project_root(start: Path) -> Path | None:
    """The nearest directory at or above start holding .labflux.toml, like git and .git."""
    start = start.absolute()
    for directory in (start, *start.parents):
        if (directory / PROJECT_FILE).is_file():
            return directory
    return None


def load_project(root: Path) -> ProjectConfig:
    path = root / PROJECT_FILE
    try:
        with path.open("rb") as stream:
            return ProjectConfig.model_validate(tomllib.load(stream))
    except (OSError, ValueError) as exc:  # TOMLDecodeError and ValidationError are ValueErrors.
        raise ProjectError(f"invalid {path}: {exc}") from exc


def session_request(
    project: ProjectConfig,
    *,
    cpu: float | None = None,
    memory_mib: int | None = None,
    gpu: int | None = None,
) -> SessionRequest:
    """Flags first, then [session], then the ResourceRequest defaults; raises ValueError."""
    values = project.session.model_dump(
        include={"cpu", "memory_mib", "gpu", "gpu_model", "min_vram_mib"}, exclude_none=True
    )
    flags = {"cpu": cpu, "memory_mib": memory_mib, "gpu": gpu}
    values.update({key: value for key, value in flags.items() if value is not None})
    return SessionRequest(
        resources=ResourceRequest(**values),
        image=project.session.image,
        datasets=project.session.datasets,
    )


# Routing and local execution -----------------------------------------------------------------


@dataclass
class Local:
    argv: list[str]


@dataclass
class Remote:
    root: Path
    project: ProjectConfig
    argv: list[str]


def route(name: str, args: list[str], cwd: Path) -> Local | Remote:
    """Where a stand-in sends `name args`; raises ProjectError for an invalid .labflux.toml."""
    argv = [name, *args]
    root = find_project_root(cwd)
    if root is None:
        return Local(argv)
    project = load_project(root)
    if name in project.route.remove:
        return Local(argv)
    if name == UV_COMMAND:
        # `uv add` and `uv lock` change pyproject.toml / uv.lock, so only `uv run` goes remote.
        return Remote(root, project, argv) if args[:1] == [UV_ROUTED_SUBCOMMAND] else Local(argv)
    if name not in DEFAULT_ROUTED_COMMANDS and name not in project.route.add:
        return Local(argv)  # A stand-in made for another project's [route] add list.
    return Remote(root, project, argv)


def rewrite_argv(argv: list[str], root: Path) -> list[str]:
    """Replace a leading absolute project root in whole arguments with /workspace."""
    prefix = str(root)
    return [
        WORKSPACE + value[len(prefix) :]
        if value == prefix or value.startswith(prefix + "/")
        else value
        for value in argv
    ]


def strip_standins(path: str) -> str:
    """PATH without every directory holding the stand-in marker (nested shells included)."""
    return os.pathsep.join(
        entry
        for entry in path.split(os.pathsep)
        if not os.path.exists(os.path.join(entry or ".", STANDIN_MARKER))
    )


def exec_local(argv: list[str]) -> int:
    """Replace this process with the real local command; returns only when it cannot."""
    stripped = strip_standins(os.environ.get("PATH", os.defpath))
    real = shutil.which(argv[0], path=stripped)
    if real is None:
        print(f"{argv[0]}: command not found", file=sys.stderr)
        return EXIT_NOT_FOUND
    try:
        # Children inherit the stripped PATH, so they never route back into a stand-in.
        os.execve(real, argv, {**os.environ, "PATH": stripped})
    except OSError as exc:
        print(f"{argv[0]}: {exc.strerror}", file=sys.stderr)
        return 126


def standin_names(project: ProjectConfig | None) -> set[str]:
    names = set(DEFAULT_ROUTED_COMMANDS) | {UV_COMMAND}
    return names | set(project.route.add) if project else names


def write_standins(names: set[str]) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="labflux-standins-"))
    (directory / STANDIN_MARKER).write_text("labflux stand-in directory\n")
    # The absolute interpreter path avoids a PATH lookup (no recursion); -I keeps project files
    # in the working directory and PYTHON* variables from shadowing LabFlux's own imports.
    python = shlex.quote(sys.executable)
    for name in sorted(names):
        script = directory / name
        script.write_text(
            f'#!/bin/sh\nexec {python} -I -m labflux.cli _standin {shlex.quote(name)} -- "$@"\n'
        )
        script.chmod(0o755)
    return directory


def _keep_first(directory: Path, names: set[str]) -> str:
    """bash/zsh lines for the end of the start-up files: put the stand-ins back in front of
    PATH (conda init, pyenv and venv activation prepend to it) and warn about every routed
    name that still resolves elsewhere, which an alias or a shell function causes."""
    quoted = shlex.quote(str(directory))
    warning = (
        'labflux: warning: $(type "$__labflux_name" 2>&1 | head -n 1); that is not the '
        "LabFlux stand-in, so $__labflux_name runs locally. Remove the alias or function, "
        "or use: labflux run -- $__labflux_name"
    )
    return f"""
__labflux_first() {{
  __labflux_status=$?
  case "$PATH" in
    {quoted}:*) ;;
    *) PATH={quoted}:"$PATH" ;;
  esac
  return $__labflux_status
}}
__labflux_first
for __labflux_name in {" ".join(sorted(names))}; do
  if [ "$(command -v "$__labflux_name")" != {quoted}/"$__labflux_name" ]; then
    echo "{warning}" >&2
  fi
done
unset __labflux_name
"""


def shell_startup(shell: str, directory: Path, names: set[str], env: dict) -> list[str]:
    """Extra shell arguments (and env changes) that keep the stand-ins first on PATH after the
    user's start-up files ran and before every prompt, so a later `conda activate` does not
    shadow them either. Only bash and zsh are wrapped; other shells get PATH alone."""
    name = Path(shell).name
    if name == "bash":
        rcfile = directory / ".labflux-bashrc"
        rcfile.write_text(
            "if [ -f ~/.bashrc ]; then . ~/.bashrc; fi\n"
            + _keep_first(directory, names)
            # After the user's commands, with $? kept; element 0 when it is an array.
            + 'PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND\n}__labflux_first"\n'
        )
        return ["--rcfile", str(rcfile)]
    if name == "zsh":
        # zsh reads .zshenv and .zshrc from ZDOTDIR: ours source the user's files from the
        # user's own ZDOTDIR (which the user's .zshenv may set), then add the precmd hook.
        zdotdir = directory / ".labflux-zsh"
        zdotdir.mkdir()
        (zdotdir / ".zshenv").write_text(
            "ZDOTDIR=${LABFLUX_USER_ZDOTDIR:-$HOME}\n"
            "if [[ -f $ZDOTDIR/.zshenv ]]; then . $ZDOTDIR/.zshenv; fi\n"
            "LABFLUX_USER_ZDOTDIR=$ZDOTDIR\n"
            f"ZDOTDIR={shlex.quote(str(zdotdir))}\n"
        )
        (zdotdir / ".zshrc").write_text(
            "ZDOTDIR=$LABFLUX_USER_ZDOTDIR\n"
            "if [[ -f $ZDOTDIR/.zshrc ]]; then . $ZDOTDIR/.zshrc; fi\n"
            + _keep_first(directory, names)
            + "autoload -Uz add-zsh-hook && add-zsh-hook precmd __labflux_first\n"
        )
        if "ZDOTDIR" in env:
            env["LABFLUX_USER_ZDOTDIR"] = env["ZDOTDIR"]
        else:
            env.pop("LABFLUX_USER_ZDOTDIR", None)
        env["ZDOTDIR"] = str(zdotdir)
        return []
    say(
        f"{name} is not bash or zsh: if its start-up files change PATH, the stand-ins can be "
        "shadowed; check with `command -v python`"
    )
    return []


def run_shell(shell: str, config: Path | None, names: set[str]) -> int:
    """Run an interactive shell with the stand-ins first on PATH; returns its exit status."""
    env = dict(os.environ)
    if config is not None:
        env["LABFLUX_CONFIG"] = str(config.resolve())
    directory = write_standins(names)
    env["PATH"] = os.pathsep.join([str(directory), strip_standins(env.get("PATH", os.defpath))])
    print(
        f"labflux: stand-ins for {', '.join(sorted(names))} are first on PATH; "
        "start your AI tool here and exit the shell to leave",
        file=sys.stderr,
    )
    # Python handlers (not SIG_IGN) are reset on exec, so Ctrl-C still reaches the shell's
    # programs. SIGHUP (a closed terminal) and SIGTERM are passed on to the shell; this process
    # keeps waiting for it, so the stand-in directory is always removed below.
    children: list[subprocess.Popen] = []

    def forward(signum, _frame):
        for child in children:
            child.send_signal(signum)

    handlers = {signal.SIGINT: lambda *_: None, signal.SIGHUP: forward, signal.SIGTERM: forward}
    previous = {signum: _set_signal(signum, handler) for signum, handler in handlers.items()}
    try:
        arguments = shell_startup(shell, directory, names, env)
        children.append(subprocess.Popen([shell, *arguments], env=env))
        code = children[0].wait()
    except OSError as exc:
        print(f"labflux: cannot start {shell}: {exc.strerror or exc}", file=sys.stderr)
        code = EXIT_NOT_FOUND
    finally:
        for signum, handler in previous.items():
            _set_signal(signum, handler)
        shutil.rmtree(directory, ignore_errors=True)
    return 128 - code if code < 0 else code


def _set_signal(signum, handler):
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.signal(signum, handler) if handler is not None else None


# Write-back ----------------------------------------------------------------------------------


def state_dir(root: Path) -> Path:
    """<root>/.labflux, created with a .gitignore that keeps git from tracking its contents."""
    state = root / STATE_DIR
    if state.is_symlink():
        raise ValueError(f"{state} must be a directory, not a link")
    state.mkdir(exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    return state


def apply_writeback(
    root: Path, archive: Path, t0: int, *, max_bytes: int, max_members: int
) -> list[str]:
    """Place written-back files under root; returns notices. Raises ValueError for an unsafe
    archive. A local file changed since t0 (or a link or directory in the way) is kept and the
    remote copy is saved beside it as <name>.remote."""
    root = root.resolve()
    # Inside the root, so os.replace stays on one filesystem; .labflux is never snapshotted.
    staging = Path(tempfile.mkdtemp(prefix="writeback-", dir=state_dir(root)))
    notices = []
    try:
        extract_archive(archive, staging, max_bytes=max_bytes, max_members=max_members)
        for directory, subdirs, files in os.walk(staging):
            subdirs.sort()
            for name in sorted(files):
                source = Path(directory) / name
                relative = source.relative_to(staging).as_posix()
                if writeback_excluded(relative):
                    continue
                notice = _place(root, relative, source, t0)
                if notice:
                    notices.append(notice)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return notices


def _place(root: Path, relative: str, source: Path, t0: int) -> str | None:
    parts = relative.split("/")
    parent = root
    for part in parts[:-1]:
        parent = parent / part
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir()
            continue
        if not stat.S_ISDIR(info.st_mode):  # Never write through a local link.
            shown = parent.relative_to(root).as_posix()
            return f"{relative} was not written back: {shown} is not a directory in the project"
    target = parent / parts[-1]
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    try:
        if info is not None and (
            stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) or info.st_mtime_ns >= t0
        ):
            os.replace(source, target.with_name(target.name + REMOTE_SUFFIX))
            return (
                f"{relative} changed locally during the command; "
                f"the remote copy is saved as {relative}{REMOTE_SUFFIX}"
            )
        os.replace(source, target)
    except OSError as exc:
        return f"{relative} was not written back: {exc.strerror or exc}"
    return None


# Remote-only files ---------------------------------------------------------------------------
#
# Files over the write-back limit stay in the session workspace. Every CommandResult carries
# the session's current list; the client mirrors it in .labflux/remote-files.txt so that an AI
# tool can look it up after the one-line hint has left its context.


def mib(size: int) -> str:
    return f"{size / 1024**2:.1f} MB"


def _total(files: list[SkippedFile], count: int) -> str:
    """Like "3 files (812.0 MB)"; "over" when more files exist than the result listed."""
    size = mib(sum(item.size for item in files))
    return (
        f"{count} file{'' if count == 1 else 's'} ({'over ' if count > len(files) else ''}{size})"
    )


def _names(files: list[SkippedFile], count: int) -> str:
    names = ", ".join(item.path for item in files[:HINT_NAMES])
    return names + (", ..." if count > HINT_NAMES else "")


def update_remote_files(root: Path, files: list[SkippedFile], count: int) -> str | None:
    """Rewrite .labflux/remote-files.txt from the session's remote-only list (removing it when
    the list is empty); returns the one-line hint when the list is non-empty and changed."""
    path = root / REMOTE_FILES
    if not count:
        path.unlink(missing_ok=True)
        return None
    more = count - len(files)
    lines = [
        "# Files that exist only in the remote LabFlux session: they were over the write-back",
        "# limit, so they were not copied to this machine. Commands routed to the session",
        "# (python, pytest, uv run, labflux run -- ...) can read them at the same path.",
        "# They are deleted when the session ends; write files you want to keep to /outputs.",
        f"# {_total(files, count)}"
        + (f"; only the first {len(files)} are listed, {more} more exist" if more > 0 else ""),
        *(f"{item.path}\t{item.size} bytes ({mib(item.size)})" for item in files),
    ]
    text = "\n".join(lines) + "\n"
    try:
        if path.read_text() == text:
            return None
    except OSError:  # No list yet.
        pass
    state_dir(root)
    path.write_text(text)
    return (
        f"{_total(files, count)} exist{'s' if count == 1 else ''} only in the remote session: "
        f"{_names(files, count)}; list: {REMOTE_FILES}; write files you want to keep to /outputs"
    )


def clear_remote_files(root: Path) -> None:
    try:
        (root / REMOTE_FILES).unlink(missing_ok=True)
    except OSError:
        pass


def read_remote_files(root: Path) -> str | None:
    try:
        return (root / REMOTE_FILES).read_text()
    except OSError:
        return None


# `labflux run` -------------------------------------------------------------------------------


class FastLaneError(Exception):
    """A LabFlux-side failure: printed as `labflux: <message>`, exit status 125."""


class Interrupts:
    """Signals received during a command. Handlers only record them; the loops act on them."""

    def __init__(self) -> None:
        self.sigint = 0
        self.terminate = 0  # SIGTERM or SIGHUP number.

    @contextmanager
    def installed(self):
        def handler(signum, _frame):
            if signum == signal.SIGINT:
                self.sigint += 1
            else:
                self.terminate = signum

        previous = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(signum, handler)
        try:
            yield self
        finally:
            for signum, value in previous.items():
                signal.signal(signum, value)

    @property
    def any(self) -> bool:
        return bool(self.sigint or self.terminate)

    @property
    def abort(self) -> bool:
        """Leave now without waiting for the result: a second Ctrl-C, SIGTERM or SIGHUP."""
        return self.sigint >= 2 or bool(self.terminate)

    @property
    def exit_code(self) -> int:
        return 128 + self.terminate if self.terminate else EXIT_CANCELLED


def say(text: str) -> None:
    print(f"labflux: {text}", file=sys.stderr, flush=True)


def run_command(
    settings,
    root: Path,
    project: ProjectConfig,
    argv: list[str],
    cwd: Path,
    *,
    cpu: float | None = None,
    memory_mib: int | None = None,
    gpu: int | None = None,
) -> int:
    """Run argv in the user's session (leasing one when needed); returns the exit status."""
    logging.getLogger("websockets").setLevel(logging.WARNING)
    root = root.absolute()
    try:
        request = session_request(project, cpu=cpu, memory_mib=memory_mib, gpu=gpu)
        relative = cwd.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        say(str(exc))
        return EXIT_ERROR
    interrupts = Interrupts()
    try:
        with (
            interrupts.installed(),
            httpx.Client(
                base_url=settings.scheduler_url,
                headers={"Authorization": f"Bearer {settings.user_token}"},
                timeout=30,
                trust_env=False,
            ) as client,
        ):
            run = _Run(
                settings, client, interrupts, root, relative, argv, project.session.busy_wait_s
            )
            return run.run(request)
    except FastLaneError as exc:
        say(str(exc))
    except httpx.HTTPError as exc:
        say(f"request to the scheduler failed: {exc}")
    return EXIT_ERROR


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return str(detail) if detail else f"HTTP {response.status_code}"


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class _Run:
    def __init__(self, settings, client, interrupts, root, cwd, argv, busy_wait_s):
        self.settings = settings
        self.client = client
        self.interrupts = interrupts
        self.root = root
        self.cwd = cwd
        self.argv = argv
        self.busy_wait_s = busy_wait_s
        self.links_noted = False

    def run(self, request: SessionRequest) -> int:
        created_id = None
        for attempt in range(2):
            view = self.open(request)
            if view.created:
                created_id = view.id
                clear_remote_files(self.root)  # The list belonged to an earlier session.
            view = self.wait(view, created=view.id == created_id)
            if isinstance(view, int):
                return view
            outcome = self.execute(view)
            # The session ended or restarted between the poll and the command: lease again once.
            if isinstance(outcome, ErrorMessage) and outcome.code == "not_ready" and not attempt:
                continue
            if isinstance(outcome, ErrorMessage):
                raise FastLaneError(f"{outcome.code}: {outcome.message}")
            return outcome
        raise AssertionError("unreachable")

    def open(self, request: SessionRequest) -> SessionView:
        response = self.client.post("/api/sessions", json=request.model_dump())
        if response.is_error:
            raise FastLaneError(_detail(response))
        return SessionView.model_validate(response.json())

    def current(self, view: SessionView) -> SessionView:
        response = self.client.get("/api/sessions/current")
        if response.status_code == 200 and response.json().get("id") == view.id:
            return SessionView.model_validate(response.json())
        # The session is no longer the current one: it ended. The job row keeps the reason.
        job = self.client.get(f"/api/jobs/{view.id}")
        if job.is_error:
            raise FastLaneError(f"session {view.id[:8]} is gone")
        value = job.json()
        return view.model_copy(update={"state": value["state"], "reason": value["reason"]})

    def wait(self, view: SessionView, *, created: bool) -> SessionView | int:
        noted = False
        while view.state != SESSION_READY_STATE:
            if self.interrupts.any:
                if created:
                    try:
                        self.client.post("/api/sessions/current/close")
                    except httpx.HTTPError:
                        pass
                return self.interrupts.exit_code
            if view.state == "UNKNOWN":
                raise FastLaneError(
                    f"session {view.id[:8]} is UNKNOWN (its node is unreachable): {view.reason}; "
                    "close it with `labflux session close`"
                )
            if view.state in ENDED_STATES:
                raise FastLaneError(f"session {view.id[:8]} ended ({view.state}): {view.reason}")
            if not noted:
                say(f"waiting for a session: {view.reason}")
                noted = True
            time.sleep(POLL_S)
            view = self.current(view)
        return view

    def execute(self, view: SessionView) -> int | ErrorMessage:
        """Run the command, waiting up to busy_wait_s while the session runs another one (AI
        tools start commands in parallel). Every attempt takes a fresh snapshot, so the command
        sees what the one before it wrote back."""
        deadline = time.monotonic() + self.busy_wait_s
        noted = False
        while True:
            outcome = self.attempt(view)
            if not isinstance(outcome, ErrorMessage) or outcome.code != "busy":
                return outcome
            if time.monotonic() >= deadline:
                raise FastLaneError(
                    f"the session was still running another command after {self.busy_wait_s:g} s "
                    "(busy_wait_s under [session] in .labflux.toml); run the command again"
                )
            if not noted:
                say("the session is running another command; waiting for it to finish")
                noted = True
            time.sleep(BUSY_RETRY_S)
            if self.interrupts.any:
                return self.interrupts.exit_code

    def attempt(self, view: SessionView) -> int | ErrorMessage:
        t0 = time.time_ns() - MTIME_SLACK_NS
        with tempfile.TemporaryDirectory(prefix="labflux-run-") as temp:
            snapshot = Path(temp) / "snapshot.tar.gz"
            try:
                links = pack_directory(self.root, snapshot)
            except (ValueError, OSError) as exc:
                raise FastLaneError(f"cannot snapshot the project: {exc}") from exc
            if links and not self.links_noted:
                self.links_noted = True
                say(
                    f"{len(links)} symbolic link{'' if len(links) == 1 else 's'} in the project "
                    f"{'was' if len(links) == 1 else 'were'} not sent to the session (links are "
                    f"not followed): {', '.join(links[:HINT_NAMES])}"
                    + (", ..." if len(links) > HINT_NAMES else "")
                )
            size = snapshot.stat().st_size
            if size > self.settings.max_source_bytes:
                raise FastLaneError(
                    f"the project snapshot is {mib(size)}, above max_source_bytes "
                    f"({mib(self.settings.max_source_bytes)}); exclude large files in "
                    ".labfluxignore"
                )
            try:
                request = ExecRequest(
                    argv=rewrite_argv(self.argv, self.root),
                    cwd=self.cwd,
                    snapshot_size=size,
                    snapshot_sha256=_sha256(snapshot),
                )
            except ValueError as exc:
                raise FastLaneError(f"invalid command: {exc}") from exc
            deadline = time.monotonic() + WORKER_RETRY_S
            while True:
                if self.interrupts.any:
                    return self.interrupts.exit_code
                outcome = self.exchange(view.id, request, snapshot, Path(temp), t0)
                if (
                    isinstance(outcome, ErrorMessage)
                    and outcome.code == "worker_unavailable"
                    and time.monotonic() < deadline
                ):
                    time.sleep(1)
                    continue
                return outcome

    def exchange(
        self, session_id: str, request: ExecRequest, snapshot: Path, temp: Path, t0: int
    ) -> int | ErrorMessage:
        from websockets.exceptions import ConnectionClosed, WebSocketException
        from websockets.sync.client import connect

        base = self.settings.scheduler_url.rstrip("/")
        url = re.sub(r"^http", "ws", base) + f"/api/sessions/{session_id}/ws"
        try:
            socket = connect(
                url,
                additional_headers={"Authorization": f"Bearer {self.settings.user_token}"},
                proxy=None,
                close_timeout=2,
            )
        except (OSError, WebSocketException) as exc:
            raise FastLaneError(f"cannot open the command connection: {exc}") from exc
        with socket:
            try:
                socket.send(request.model_dump_json())
                with snapshot.open("rb") as stream:
                    for text, final in iter_chunks(stream):
                        if self.interrupts.abort:
                            return self.interrupts.exit_code
                        socket.send(SnapshotChunk(data=text, final=final).model_dump_json())
            except ConnectionClosed:
                pass  # The Scheduler replied early (e.g. too_large); read what it sent.
            return self.receive(socket, temp / "writeback.tar.gz", t0)

    def receive(self, socket, writeback: Path, t0: int) -> int | ErrorMessage:
        from websockets.exceptions import ConnectionClosed

        cancel_sent = False
        with writeback.open("wb") as archive:
            while True:
                if self.interrupts.abort:
                    # Closing the socket stops the command remotely; the session stays.
                    return self.interrupts.exit_code
                if self.interrupts.sigint and not cancel_sent:
                    cancel_sent = True
                    try:
                        socket.send(CancelRequest().model_dump_json())
                    except ConnectionClosed:
                        pass
                try:
                    raw = socket.recv(timeout=RECV_S)
                except TimeoutError:
                    continue
                except ConnectionClosed as exc:
                    raise FastLaneError(
                        "the connection to the scheduler closed before the command finished"
                    ) from exc
                try:
                    message = parse_server_message(json.loads(raw))
                except ValueError as exc:
                    raise FastLaneError(f"invalid message from the scheduler: {exc}") from exc
                if isinstance(message, Output):
                    target = sys.stdout if message.stream == "stdout" else sys.stderr
                    try:
                        target.buffer.write(decode(message.data))
                        target.buffer.flush()
                    except BrokenPipeError:
                        return EXIT_BROKEN_PIPE
                elif isinstance(message, Notice):
                    say(message.text)
                elif isinstance(message, WritebackChunk):
                    archive.write(decode(message.data))
                elif isinstance(message, CommandResult):
                    archive.close()
                    return self.finish(message, writeback, t0)
                else:
                    return message

    def finish(self, result: CommandResult, writeback: Path, t0: int) -> int:
        if result.reason:
            say(result.reason)
        self.note_remote_files(result)
        if result.writeback_files:
            if (
                writeback.stat().st_size != result.writeback_size
                or _sha256(writeback) != result.writeback_sha256
            ):
                say("the write-back archive failed its checksum; no files were written back")
                return EXIT_ERROR
            try:
                notices = apply_writeback(
                    self.root,
                    writeback,
                    t0,
                    max_bytes=self.settings.max_extracted_bytes,
                    max_members=self.settings.max_archive_members,
                )
            except (ValueError, OSError) as exc:
                say(f"the write-back was rejected: {exc}")
                return EXIT_ERROR
            for notice in notices:
                say(notice)
        if result.outcome == "cancelled" and not result.session_ended:
            say("the command was stopped; the session is still running")
        if result.session_ended and result.outcome != "exited":
            say("the session was closed; the next command starts a new one")
        return exit_status(result)

    def note_remote_files(self, result: CommandResult) -> None:
        """Mirror the session's remote-only list locally and print at most one line about it."""
        files, count = result.skipped, result.skipped_count
        if result.session_ended:  # The session's files are gone with it.
            clear_remote_files(self.root)
            if count:
                say(
                    f"{_total(files, count)} over the write-back limit ended with the session "
                    f"without being written back: {_names(files, count)}"
                )
            return
        try:
            hint = update_remote_files(self.root, files, count)
        except (OSError, ValueError) as exc:
            say(f"cannot update {REMOTE_FILES}: {exc}")
            return
        if hint:
            say(hint)
