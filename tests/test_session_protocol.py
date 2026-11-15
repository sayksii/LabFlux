"""The fast-lane message models and helpers shared by the CLI, Scheduler and Worker (M9)."""

import io

import pytest
from pydantic import ValidationError

from labflux.session_protocol import (
    CHUNK_BYTES,
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_TIMEOUT,
    CancelRequest,
    CommandResult,
    ErrorMessage,
    ExecRequest,
    Output,
    SessionRequest,
    SnapshotChunk,
    WorkerExec,
    decode,
    encode,
    exit_status,
    iter_chunks,
    parse_client_message,
    parse_server_message,
    parse_worker_command,
    parse_worker_message,
    safe_relative,
    writeback_excluded,
)

ID = "a" * 32
SHA = "b" * 64


def test_messages_round_trip_through_the_discriminated_parsers():
    request = ExecRequest(
        argv=["pytest", "-q"], cwd="./tests/", snapshot_size=3, snapshot_sha256=SHA
    )
    assert request.cwd == "tests"
    assert parse_client_message(request.model_dump()) == request
    assert isinstance(parse_client_message({"type": "cancel"}), CancelRequest)
    chunk = parse_client_message({"type": "snapshot_chunk", "data": encode(b"abc"), "final": True})
    assert isinstance(chunk, SnapshotChunk) and chunk.id == ""

    forwarded = WorkerExec(
        **request.model_dump(exclude={"type"}),
        id=ID,
        attempt_id="c" * 32,
        timeout_s=60,
        sync_timeout_s=900,
        writeback_limit_bytes=100,
        output_limit_bytes=100,
    )
    assert parse_worker_command(forwarded.model_dump()) == forwarded
    assert parse_worker_command(chunk.model_copy(update={"id": ID}).model_dump()).id == ID

    output = Output(id=ID, stream="stderr", data=encode(b"\x1b[31mred"))
    assert parse_worker_message(output.model_dump()) == output
    result = CommandResult(id=ID, outcome="exited", exit_code=3)
    assert parse_server_message(result.model_dump()) == result
    error = parse_server_message({"type": "error", "code": "busy", "message": "one at a time"})
    assert isinstance(error, ErrorMessage)
    with pytest.raises(ValidationError):
        parse_worker_message(error.model_dump())  # Only the Scheduler reports errors.


@pytest.mark.parametrize(
    "value",
    [
        {"type": "exec", "argv": [""], "snapshot_size": 0, "snapshot_sha256": SHA},
        {"type": "exec", "argv": ["a\x00"], "snapshot_size": 0, "snapshot_sha256": SHA},
        {"type": "exec", "argv": ["x" * 200_000], "snapshot_size": 0, "snapshot_sha256": SHA},
        {
            "type": "exec",
            "argv": ["py"],
            "cwd": "../up",
            "snapshot_size": 0,
            "snapshot_sha256": SHA,
        },
        {"type": "exec", "argv": ["py"], "cwd": "/etc", "snapshot_size": 0, "snapshot_sha256": SHA},
        {"type": "exec", "argv": ["py"], "snapshot_size": -1, "snapshot_sha256": SHA},
        {"type": "exec", "argv": ["py"], "snapshot_size": 0, "snapshot_sha256": SHA, "env": {}},
        {"type": "snapshot_chunk", "data": "A" * (4 * CHUNK_BYTES), "final": True},
        {"type": "snapshot_chunk", "id": "../x", "data": "", "final": True},
        {"type": "unknown"},
    ],
)
def test_client_messages_reject_unsafe_or_oversized_values(value):
    with pytest.raises(ValidationError):
        parse_client_message(value)


def test_result_consistency_and_exit_statuses():
    with pytest.raises(ValidationError):
        CommandResult(id=ID, outcome="exited")
    with pytest.raises(ValidationError):
        CommandResult(id=ID, outcome="exited", exit_code=0, writeback_files=1)
    assert exit_status(CommandResult(id=ID, outcome="exited", exit_code=0)) == 0
    assert exit_status(CommandResult(id=ID, outcome="exited", exit_code=137)) == 137
    assert exit_status(CommandResult(id=ID, outcome="timeout", session_ended=True)) == EXIT_TIMEOUT
    assert exit_status(CommandResult(id=ID, outcome="cancelled")) == EXIT_CANCELLED
    assert exit_status(CommandResult(id=ID, outcome="error")) == EXIT_ERROR
    assert exit_status(ErrorMessage(code="not_ready", message="queued")) == EXIT_ERROR


def test_chunks_cover_the_stream_and_decode_back():
    data = bytes(range(256)) * 2048  # 512 KiB: exactly two full chunks.
    chunks = list(iter_chunks(io.BytesIO(data)))
    assert [final for _, final in chunks] == [False, True]
    assert b"".join(decode(text) for text, _ in chunks) == data
    assert list(iter_chunks(io.BytesIO(b""))) == [("", True)]
    assert [final for _, final in iter_chunks(io.BytesIO(b"x" * (CHUNK_BYTES + 1)))] == [
        False,
        True,
    ]
    with pytest.raises(ValueError):
        decode("not base64!")


def test_relative_paths_and_writeback_exclusions():
    assert safe_relative("") == "" and safe_relative(".") == ""
    assert safe_relative("a/./b/") == "a/b"
    for unsafe in ("../a", "a/../../b", "/abs", "a\\b", "a\x00"):
        with pytest.raises(ValueError):
            safe_relative(unsafe)
    assert writeback_excluded(".venv/lib/x.py")
    assert writeback_excluded("pkg/__pycache__/m.cpython-312.pyc")
    assert writeback_excluded("tests/.pytest_cache/v/cache/lastfailed")
    assert writeback_excluded("stale.pyc")
    assert not writeback_excluded("outputs/results.json")
    assert not writeback_excluded("venv.txt")


def test_session_request_defaults_and_resource_validation():
    assert SessionRequest().resources.gpu == 0
    assert SessionRequest.model_validate({"resources": {"gpu": 1, "cpu": 4}}).image is None
    with pytest.raises(ValidationError):
        SessionRequest.model_validate({"resources": {"gpu_model": "A100"}})
