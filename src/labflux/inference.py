"""Inference Gateway (M10, G1): OpenAI-compatible requests relayed to model servers.

A client sends a request to the Scheduler's /v1 API with its user token. The Gateway finds the
service of the requested model, starting it if needed, and relays the request over the doorbell
connection of the Worker that runs it (T4); that Worker sends it to the model container on its
own host and streams the response back the same way. The container publishes no port. Nothing
here is durable: a Scheduler restart fails the requests in flight, which clients send again.

Per request, in order:
  Scheduler -> Worker: InferRequest, InferBody... (final last), [InferCancel]
  Worker -> Scheduler: InferHead, InferChunk..., InferEnd (last); or only an InferEnd on error
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
from typing import Annotated, Literal

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from labflux.hub import Hub
from labflux.scheduler import Scheduler, ServiceError
from labflux.session_protocol import CHUNK_TEXT_LIMIT, decode, iter_chunks

log = logging.getLogger("labflux.inference")

# /v1/<path> requests the Gateway relays; /v1/models is answered by the Scheduler itself.
RELAYED_PATHS = frozenset({"chat/completions", "completions", "embeddings"})
POLL_S = 0.5  # How often a waiting request checks whether its model is serving yet.
LIVENESS_S = 1.0  # How often a relayed request checks that the Worker is still connected.
# Response messages a slow client may leave unread before its request is cut off (each holds
# at most CHUNK_BYTES), so one client cannot make the Scheduler buffer without limit.
MAX_QUEUED_MESSAGES = 64
RETRY_AFTER_S = 10
REQUEST_ID = r"^[0-9a-f]{32}$"


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InferRequest(Message):
    type: Literal["infer"] = "infer"
    id: str = Field(pattern=REQUEST_ID)
    attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    path: Literal["chat/completions", "completions", "embeddings"]
    content_type: str = Field(default="application/json", max_length=256)
    body_size: int = Field(ge=0)
    read_timeout_s: float = Field(gt=0)


class InferBody(Message):
    type: Literal["infer_body"] = "infer_body"
    id: str = Field(pattern=REQUEST_ID)
    data: str = Field(max_length=CHUNK_TEXT_LIMIT)
    final: bool = False


class InferCancel(Message):
    type: Literal["infer_cancel"] = "infer_cancel"
    id: str = Field(pattern=REQUEST_ID)


class InferHead(Message):
    type: Literal["infer_head"] = "infer_head"
    id: str = Field(pattern=REQUEST_ID)
    status: int = Field(ge=100, le=599)
    content_type: str = Field(default="application/json", max_length=256)


class InferChunk(Message):
    type: Literal["infer_chunk"] = "infer_chunk"
    id: str = Field(pattern=REQUEST_ID)
    data: str = Field(max_length=CHUNK_TEXT_LIMIT)


class InferEnd(Message):
    type: Literal["infer_end"] = "infer_end"
    id: str = Field(pattern=REQUEST_ID)
    # None: the response is complete. Otherwise why it ended early (or never started).
    error: str | None = Field(default=None, max_length=2048)


_to_worker = TypeAdapter(
    Annotated[InferRequest | InferBody | InferCancel, Field(discriminator="type")]
)
_from_worker = TypeAdapter(
    Annotated[InferHead | InferChunk | InferEnd, Field(discriminator="type")]
)
TO_WORKER_TYPES = frozenset({"infer", "infer_body", "infer_cancel"})


def parse_to_worker(message) -> InferRequest | InferBody | InferCancel:
    try:
        return _to_worker.validate_python(message)
    except Exception as exc:
        raise ValueError(str(exc)[:500]) from exc


def parse_from_worker(message) -> InferHead | InferChunk | InferEnd:
    try:
        return _from_worker.validate_python(message)
    except Exception as exc:
        raise ValueError(str(exc)[:500]) from exc


def openai_error(status: int, message: str, kind: str, retry_after: float | None = None):
    """An error in the shape OpenAI clients parse; 503 carries Retry-After so they retry."""
    headers = {"Retry-After": str(max(1, round(retry_after)))} if retry_after else None
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": kind, "code": status}},
        headers=headers,
    )


class TruncatedResponse(Exception):
    """A relayed response that ended early after its status line was sent. Raising it makes
    the server drop the connection without the final chunk, so the client sees an incomplete
    body and can retry, instead of a cut-off answer that looks complete."""


class RelayedResponse(StreamingResponse):
    """A streaming response whose cleanup runs however the response ends, including a client
    that disconnects before the first byte (when Starlette skips background tasks)."""

    def __init__(self, *args, on_close, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_close = on_close

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.on_close()


async def read_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ServiceError(413, f"request body exceeds {limit} bytes")
    return bytes(body)


async def wait_until_serving(service: Scheduler, hub: Hub, name: str) -> dict | Response:
    """Start the model if needed and wait up to its cold_wait_s for it to serve (plan 10.6);
    an error response when it does not."""
    config = service.settings.models[name]
    deadline = time.monotonic() + config.cold_wait_s
    while True:
        view = await asyncio.to_thread(service.ensure_model, name)
        if view["state"] == "serving" and view["node_id"] in hub.workers:
            return view
        if view["state"] == "failed" and view["retry_at"]:
            return openai_error(
                503,
                f"model {name} failed: {view['reason']}; it is started again after "
                f"{time.strftime('%H:%M:%S', time.localtime(view['retry_at']))}",
                "model_failed",
                view["retry_at"] - service.clock(),
            )
        if time.monotonic() >= deadline:
            detail = view["reason"] or view["state"]
            if view["state"] == "serving":
                detail = f"worker {view['node_id']} is not connected"
            return openai_error(
                503,
                f"model {name} is not serving yet ({view['state']}: {detail}); retry later",
                "model_not_ready",
                RETRY_AFTER_S,
            )
        await asyncio.sleep(POLL_S)


async def forward(request: Request, path: str, service: Scheduler, hub: Hub) -> Response:
    """Relay one OpenAI-compatible request to the model it names (plan 10.5)."""
    settings = service.settings
    body = await read_body(request, settings.gateway_max_request_bytes)
    try:
        name = json.loads(body)["model"]
        if not isinstance(name, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return openai_error(400, "the body must be a JSON object with a model name", "invalid")
    if name not in settings.models:
        return openai_error(404, f"model {name!r} is not configured", "model_not_found")
    view = await wait_until_serving(service, hub, name)
    if isinstance(view, Response):
        return view

    job_id, node_id = view["job_id"], view["node_id"]
    peer = hub.workers.get(node_id)
    request_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    overflow = object()  # Queued once when the client leaves too many messages unread.
    overflowed = False
    finished = False  # The Worker ended the request itself, so there is nothing to cancel.
    closed = False

    def receive(_node: str, message: dict) -> None:
        nonlocal overflowed
        if overflowed:
            return
        if queue.qsize() >= MAX_QUEUED_MESSAGES:
            overflowed = True
            queue.put_nowait(overflow)
            return
        queue.put_nowait(message)

    async def close() -> None:
        """Runs exactly once, after the response ends however it ends (plan 10.8)."""
        nonlocal closed
        if closed:
            return
        closed = True
        unsubscribe()
        if not finished:
            hub.send(node_id, InferCancel(id=request_id).model_dump())
        await asyncio.to_thread(service.end_request, job_id)

    async def next_message() -> InferHead | InferChunk | InferEnd:
        """The next message from the Worker. An InferEnd from the Worker sets `finished`; one
        made here (Worker gone or silent, client too slow) leaves the cancel to close()."""
        nonlocal finished
        silent_until = time.monotonic() + settings.gateway_read_timeout_s + 30
        while True:
            try:
                raw = await asyncio.wait_for(queue.get(), LIVENESS_S)
            except TimeoutError:
                if hub.workers.get(node_id) is not peer:
                    return InferEnd(id=request_id, error=f"lost the connection to {node_id}")
                if time.monotonic() > silent_until:
                    return InferEnd(id=request_id, error="no response from the model in time")
                continue
            if raw is overflow:
                return InferEnd(id=request_id, error="the client is not reading the response")
            try:
                message = parse_from_worker(raw)
            except ValueError:
                log.warning("dropped a malformed inference message from %s", node_id)
                continue
            finished = isinstance(message, InferEnd)
            return message

    unsubscribe = hub.subscribe(node_id, request_id, receive)
    service.begin_request(job_id)
    try:
        hub.send(
            node_id,
            InferRequest(
                id=request_id,
                attempt_id=view["attempt_id"],
                path=path,
                content_type=request.headers.get("content-type", "application/json")[:256],
                body_size=len(body),
                read_timeout_s=settings.gateway_read_timeout_s,
            ).model_dump(),
        )
        for text, final in iter_chunks(io.BytesIO(body)):
            hub.send(node_id, InferBody(id=request_id, data=text, final=final).model_dump())
        head = await next_message()
    except BaseException:
        await close()
        raise
    if not isinstance(head, InferHead):
        await close()
        error = head.error if isinstance(head, InferEnd) else "a response chunk before its head"
        return openai_error(502, f"model {name}: {error}", "upstream_error")

    async def content():
        while True:
            message = await next_message()
            if isinstance(message, InferChunk):
                yield decode(message.data)
            elif isinstance(message, InferEnd):
                if message.error:
                    # Returning would end the body like a complete one (openai-python does not
                    # require "data: [DONE]"), so abort it instead.
                    log.warning("inference response for %s ended early: %s", name, message.error)
                    raise TruncatedResponse(f"model {name}: {message.error}")
                return

    return RelayedResponse(
        content(), status_code=head.status, media_type=head.content_type, on_close=close
    )
