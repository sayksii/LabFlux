"""Host-side session workspace operations for the fast lane (D4b snapshot, D12 write-back).

Commands in the session container can create symbolic links anywhere in the bind-mounted
workspace, and a background process may still be running while the Worker works on it. Every
path is therefore walked one component at a time with O_NOFOLLOW directory descriptors, so the
Worker never reads, writes or deletes through a link a command created.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import os
import shutil
import stat
import tarfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

from labflux.archives import extract_archive
from labflux.session_protocol import (
    DEPENDENCY_FILES,
    WRITEBACK_EXCLUDED_DIRS,
    writeback_excluded,
)

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Raised when a path component is missing, a link, or not a directory.
NOT_A_DIRECTORY = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)


def _open_directory(root: Path, parts: tuple[str, ...], *, create: bool = False) -> int:
    """A descriptor for root/parts that never follows links. With create, anything in the way
    (a file or link) is replaced by a new directory; otherwise FileNotFoundError."""
    fd = os.open(root, DIRECTORY_FLAGS)
    try:
        for part in parts:
            try:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno not in NOT_A_DIRECTORY:
                    raise
                if not create:
                    raise FileNotFoundError(part) from exc
                with suppress(FileNotFoundError):
                    os.unlink(part, dir_fd=fd)  # A file or link in the way.
                os.mkdir(part, dir_fd=fd)
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def is_directory(root: Path, relative: str) -> bool:
    """Whether root/relative is a real directory reached without following links."""
    try:
        os.close(_open_directory(root, PurePosixPath(relative or ".").parts))
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _open_file(root: Path, relative: str) -> tuple[int, os.stat_result] | None:
    """An open regular file and its stat, or None if it is missing, a link or not a file."""
    *parents, name = PurePosixPath(relative).parts
    try:
        directory = _open_directory(root, tuple(parents))
    except FileNotFoundError:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError:
        return None
    finally:
        os.close(directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        return None
    return fd, info


def _files(root: Path) -> list[str]:
    found = []
    for directory, _, files in os.walk(root):
        base = Path(directory)
        found.extend((base / name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def apply_snapshot(
    workspace: Path, archive: Path, previous_files: list[str], max_bytes: int, max_members: int
) -> list[str]:
    """Make the workspace match a snapshot and return its file list (the new manifest).

    Snapshot files overwrite, files of the previous snapshot that are gone are deleted, and
    anything else (generated files, .venv) is kept. Raises ValueError for an unsafe archive
    before the workspace is touched.
    """
    staging = workspace.parent / "snapshot-new"
    shutil.rmtree(staging, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        extract_archive(archive, staging, max_bytes=max_bytes, max_members=max_members)
        new_files = _files(staging)
        for relative in sorted(set(previous_files) - set(new_files)):
            _delete(workspace, PurePosixPath(relative).parts)
        for relative in new_files:
            *parents, name = PurePosixPath(relative).parts
            directory = _open_directory(workspace, tuple(parents), create=True)
            try:
                with suppress(FileNotFoundError):
                    if stat.S_ISDIR(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
                        shutil.rmtree(name, dir_fd=directory)
                # rename replaces a link itself, never its target.
                os.replace(staging / relative, name, dst_dir_fd=directory)
            finally:
                os.close(directory)
        return new_files
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _delete(workspace: Path, parts: tuple[str, ...]) -> None:
    """Unlink a file or link (never a directory), then remove parents that became empty."""
    try:
        directory = _open_directory(workspace, parts[:-1])
    except FileNotFoundError:
        return
    try:
        info = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            return
        os.unlink(parts[-1], dir_fd=directory)
    except FileNotFoundError:
        return
    finally:
        os.close(directory)
    for depth in range(len(parts) - 1, 0, -1):
        try:
            directory = _open_directory(workspace, parts[: depth - 1])
        except FileNotFoundError:
            return
        try:
            os.rmdir(parts[depth - 1], dir_fd=directory)
        except OSError:
            return  # Not empty (or not a directory): stop pruning.
        finally:
            os.close(directory)


def scan(workspace: Path) -> dict[str, tuple[int, int]]:
    """{relative path: (size, mtime_ns)} of the regular files change detection looks at."""
    found = {}
    for directory, subdirs, files in os.walk(workspace):
        subdirs[:] = [name for name in subdirs if name not in WRITEBACK_EXCLUDED_DIRS]
        base = Path(directory)
        for name in files:
            relative = (base / name).relative_to(workspace).as_posix()
            if writeback_excluded(relative):
                continue
            try:
                info = os.lstat(base / name)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                found[relative] = (info.st_size, info.st_mtime_ns)
    return found


def pack_files(workspace: Path, relatives: list[str], target: Path) -> list[str]:
    """Write a reproducible gzip tar of these regular files; returns the ones packed (a file
    that vanished or became a link is skipped)."""
    packed = []
    with (
        target.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as zipped,
        tarfile.open(fileobj=zipped, mode="w") as archive,
    ):
        for relative in relatives:
            opened = _open_file(workspace, relative)
            if opened is None:
                continue
            fd, info = opened
            with os.fdopen(fd, "rb") as stream:
                member = tarfile.TarInfo(relative)
                member.size = info.st_size
                member.mode = info.st_mode & 0o777
                archive.addfile(member, stream)
            packed.append(relative)
    return packed


def dependency_hash(workspace: Path) -> str | None:
    """sha256 over the dependency files present, or None when there are none."""
    digest = hashlib.sha256()
    found = False
    for name in DEPENDENCY_FILES:
        opened = _open_file(workspace, name)
        if opened is None:
            continue
        found = True
        with os.fdopen(opened[0], "rb") as stream:
            data = stream.read()
        digest.update(f"{name}\0{len(data)}\0".encode() + data)
    return digest.hexdigest() if found else None
