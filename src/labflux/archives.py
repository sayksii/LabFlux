"""Small reproducible source snapshots and bounded, link-free archive extraction."""

from __future__ import annotations

import gzip
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath

from pathspec import GitIgnoreSpec

ALWAYS_EXCLUDED = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".labflux"}


def pack_directory(root: Path, target: Path, *, source: bool = True) -> None:
    root = root.resolve()
    patterns = []
    if source:
        patterns = ["data/", "datasets/", "outputs/", ".env", ".env.*", "*.pyc", "*.sqlite*"]
        for filename in (".gitignore", ".labfluxignore"):
            path = root / filename
            if path.is_file():
                patterns.extend(path.read_text().splitlines())
    ignored = GitIgnoreSpec.from_lines(patterns)
    with (
        target.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as zipped,
    ):
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            for directory, subdirs, files in os.walk(root):
                base = Path(directory)
                kept = []
                for name in sorted(subdirs):
                    path = base / name
                    relative = path.relative_to(root).as_posix() + "/"
                    if source and (name in ALWAYS_EXCLUDED or ignored.match_file(relative)):
                        continue
                    if path.is_symlink():
                        raise ValueError(f"archive cannot contain symbolic links: {relative}")
                    kept.append(name)
                subdirs[:] = kept
                for name in sorted(files):
                    path = base / name
                    relative = path.relative_to(root).as_posix()
                    if source and (name in ALWAYS_EXCLUDED or ignored.match_file(relative)):
                        continue
                    if path.is_symlink() or not path.is_file():
                        raise ValueError(
                            f"archive cannot contain links or special files: {relative}"
                        )
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    info.mode &= 0o777
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)


def extract_archive(archive: Path, destination: Path, *, max_bytes: int, max_members: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        for index, member in enumerate(source, start=1):
            name = PurePosixPath(member.name)
            if (
                index > max_members
                or name.is_absolute()
                or ".." in name.parts
                or "\\" in member.name
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"unsafe archive member: {member.name}")
            total += member.size
            if member.size < 0 or total > max_bytes:
                raise ValueError("archive exceeds extraction limits")
            target = destination.joinpath(*name.parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError("archive escapes its destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                incoming = source.extractfile(member)
                if incoming is None:
                    raise ValueError("unreadable archive member")
                with incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=65536)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
