import io
import tarfile

import pytest

from labflux.archives import extract_archive, pack_directory


def test_snapshot_is_reproducible_and_ignores_generated_and_private_files(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text("print('hello')")
    (root / ".env").write_text("PRIVATE=not-for-upload")
    (root / ".labfluxignore").write_text("scratch.txt\n")
    (root / "scratch.txt").write_text("ignore")
    for directory in (".git", ".venv", "data", "outputs"):
        (root / directory).mkdir()
        (root / directory / "private").write_text("ignored")
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    pack_directory(root, first)
    pack_directory(root, second)
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        assert set(archive.getnames()) == {"main.py", ".labfluxignore"}
    destination = tmp_path / "extracted"
    extract_archive(first, destination, max_bytes=10000, max_members=10)
    assert (destination / "main.py").read_text() == "print('hello')"


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", "file"),
        ("/tmp/escape", "file"),
        ("link", "symlink"),
        ("hard", "hardlink"),
        ("device", "device"),
    ],
)
def test_reject_path_escape_links_and_devices(tmp_path, name, kind):
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = {
            "file": tarfile.REGTYPE,
            "symlink": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
            "device": tarfile.CHRTYPE,
        }[kind]
        member.linkname = "../../escape"
        archive.addfile(member, io.BytesIO(b""))
    with pytest.raises(ValueError):
        extract_archive(path, tmp_path / "target", max_bytes=100, max_members=10)
    assert not (tmp_path / "escape").exists()


def test_extraction_limit_and_no_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"x" * 100)
    archive = tmp_path / "source.tar.gz"
    pack_directory(source, archive)
    with pytest.raises(ValueError, match="limits"):
        extract_archive(archive, tmp_path / "limited", max_bytes=50, max_members=10)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "file").write_text("keep")
    with pytest.raises(FileExistsError):
        extract_archive(archive, destination, max_bytes=1000, max_members=10)
    assert (destination / "file").read_text() == "keep"
