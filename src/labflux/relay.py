"""Fast-lane command relay (T6): one CLI WebSocket per command, relayed in memory to the
session's Worker over its doorbell connection.

Nothing here is durable. A relay failure fails the command; when the Worker may still be
running it (relay loss, no result in time) the session is closed through the durable STOP
path, and its resources are released only by the attempt's confirmed FINISHED event.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress

from starlette.websockets import WebSocket, WebSocketDisconnect

from labflux.hub import Hub
from labflux.scheduler import Scheduler, ServiceError
from labflux.session_protocol import (
    WRITEBACK_OVERHEAD_BYTES,
    CancelRequest,
    CommandResult,
    ErrorMessage,
    ExecRequest,
    SnapshotChunk,
    WorkerExec,
    WritebackChunk,
    decode,
    parse_client_message,
    parse_worker_message,
)

log = logging.getLogger("labflux.relay")
FIRST_MESSAGE_TIMEOUT_S = 10.0
# Added to the sync and command limits before a command without a result counts as lost.
GUARD_SLACK_S = 60.0
POLL_S = 1.0


async def serve(
    websocket: WebSocket, owner: str, session_id: str, service: Scheduler, hub: Hub
) -> None:
    """Run one fast-lane command for an authenticated user; see session_protocol for order."""
    settings = service.settings
    await websocket.accept()
    client_gone = False

    async def send(message: dict) -> None:
        nonlocal client_gone
        if client_gone:
            return
        try:
            await websocket.send_json(message)
        except Exception:
            client_gone = True  # Keep relaying to the Worker; only the client is gone.

    def error(code: str, message: str) -> dict:
        return ErrorMessage(code=code, message=message).model_dump()

    def failed(code: str, message: str) -> None:
        """From the client reader: makes the forward loop reply with this error and stop."""
        queue.put_nowait(ErrorMessage(code=code, message=message))

    began = False  # This connection holds the session's busy mark and owes an end_command.
    stop_reason: str | None = None
    unsubscribe = None
    reader: asyncio.Task | None = None
    queue: asyncio.Queue = asyncio.Queue()
    try:
        try:
            raw = await asyncio.wait_for(websocket.receive_json(), FIRST_MESSAGE_TIMEOUT_S)
            request = parse_client_message(raw)
        except WebSocketDisconnect:
            client_gone = True
            return
        except (TimeoutError, ValueError, KeyError) as exc:
            await send(error("invalid", f"expected an exec request: {str(exc)[:500]}"))
            return
        if not isinstance(request, ExecRequest):
            await send(error("invalid", "the first message must be an exec request"))
            return
        if request.snapshot_size > settings.max_source_bytes:
            await send(error("too_large", f"snapshot exceeds {settings.max_source_bytes} bytes"))
            return
        try:
            attempt_id, node_id = await asyncio.to_thread(service.begin_command, owner, session_id)
        except ServiceError as exc:
            await send(error("not_found" if exc.status == 404 else "not_ready", exc.detail))
            return
        # Only now that begin_command has accepted the owner, so that nobody else can mark the
        # session busy. No await between the check and the add.
        if session_id in service.session_busy:
            await send(error("busy", "another command is running in this session"))
            return
        service.session_busy.add(session_id)
        began = True
        peer = hub.workers.get(node_id)
        if peer is None:
            await send(error("worker_unavailable", f"worker {node_id} is not connected"))
            return

        command_id = uuid.uuid4().hex
        unsubscribe = hub.subscribe(node_id, command_id, lambda node, item: queue.put_nowait(item))
        cancel = CancelRequest(id=command_id).model_dump()
        hub.send(
            node_id,
            WorkerExec(
                id=command_id,
                attempt_id=attempt_id,
                argv=request.argv,
                cwd=request.cwd,
                snapshot_size=request.snapshot_size,
                snapshot_sha256=request.snapshot_sha256,
                timeout_s=settings.session_command_timeout_s,
                sync_timeout_s=settings.session_sync_timeout_s,
                writeback_limit_bytes=settings.session_writeback_bytes,
                output_limit_bytes=settings.max_log_bytes,
            ).model_dump(),
        )

        async def read_client() -> None:
            nonlocal client_gone
            received = 0
            try:
                while True:
                    raw = await websocket.receive_json()
                    try:
                        message = parse_client_message(raw)
                        if isinstance(message, SnapshotChunk):
                            received += len(decode(message.data))
                    except ValueError as exc:
                        hub.send(node_id, cancel)
                        failed("invalid", str(exc)[:500])
                        return
                    if isinstance(message, ExecRequest):
                        hub.send(node_id, cancel)
                        failed("invalid", "one command per connection")
                        return
                    if isinstance(message, CancelRequest):
                        hub.send(node_id, cancel)
                        continue
                    if received > min(request.snapshot_size, settings.max_source_bytes) or (
                        message.final and received != request.snapshot_size
                    ):
                        hub.send(node_id, cancel)
                        failed("too_large", "snapshot does not match its declared size")
                        return
                    hub.send(node_id, message.model_copy(update={"id": command_id}).model_dump())
            except WebSocketDisconnect:
                # Counts as a cancel; the Worker's messages are still drained.
                client_gone = True
                hub.send(node_id, cancel)
            except (ValueError, KeyError):
                hub.send(node_id, cancel)
                failed("invalid", "messages must be JSON text")

        reader = asyncio.create_task(read_client())
        deadline = (
            time.monotonic()
            + settings.session_sync_timeout_s
            + settings.session_command_timeout_s
            + GUARD_SLACK_S
        )
        writeback_cap = settings.session_writeback_bytes + WRITEBACK_OVERHEAD_BYTES
        writeback = 0
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), POLL_S)
            except TimeoutError:
                if hub.workers.get(node_id) is not peer:
                    stop_reason = "relay connection lost during a command"
                    await send(error("worker_disconnected", f"lost the connection to {node_id}"))
                    break
                if time.monotonic() > deadline:
                    stop_reason = "no command result within the guard time"
                    hub.send(node_id, cancel)
                    await send(error("lost", "no result from the worker in time"))
                    break
                continue
            if isinstance(item, ErrorMessage):  # From the client reader.
                await send(item.model_dump())
                break
            try:
                message = parse_worker_message(item)
                size = len(decode(message.data)) if isinstance(message, WritebackChunk) else 0
            except ValueError:
                log.warning("dropped a malformed fast-lane message from %s", node_id)
                continue
            if size:
                writeback += size
                if writeback > writeback_cap:
                    hub.send(node_id, cancel)
                    await send(error("too_large", f"write-back exceeds {writeback_cap} bytes"))
                    break
            if isinstance(message, CommandResult) and settings.fast_lane_mode == "oneshot":
                # end_command closes the session below, so the client must not expect to find
                # the session's files in it afterwards.
                item = message.model_copy(update={"session_ended": True}).model_dump()
            await send(item)
            if isinstance(message, CommandResult):
                if message.session_ended:
                    # The Worker already ended the container; STOPPING at once lets the next
                    # command open a new session instead of reaching this one before FINISHED.
                    stop_reason = message.reason or "session ended by the worker"
                break
    finally:
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader
        if unsubscribe is not None:
            unsubscribe()
        try:
            if began:
                # Before releasing busy, so a lease that ran out during a long command cannot
                # expire between the two.
                await asyncio.to_thread(service.end_command, session_id, stop_reason)
        finally:
            if began:
                service.session_busy.discard(session_id)
            with suppress(Exception):
                await websocket.close(code=1000)
