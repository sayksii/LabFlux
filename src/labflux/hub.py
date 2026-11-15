"""In-memory relay over the long-lived Worker connections (doorbell T4, fast-lane relay T6).

Nothing here is durable. Batch commands still live in SQLite and are fetched and acked over
HTTP; a doorbell only tells a Worker to fetch now instead of at its next fallback poll, so a
dropped message costs latency, never correctness. All connection state is touched only on the
event loop; synchronous code (API endpoints, the scheduling loop) hands messages over with
call_soon_threadsafe and never waits for delivery.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable

from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("labflux.hub")

# handler(node_id, message); may be async. Runs on the event loop, so it must not block.
Handler = Callable[[str, dict], Awaitable[None] | None]


class Peer:
    """One accepted connection with an ordered outgoing queue."""

    def __init__(self, websocket: WebSocket, instance_id: str):
        self.websocket = websocket
        self.instance_id = instance_id
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def write(self) -> None:
        while (message := await self.queue.get()) is not None:
            await self.websocket.send_json(message)


class Hub:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.workers: dict[str, Peer] = {}
        self.handlers: dict[str, Handler] = {}
        self.subscriptions: dict[tuple[str, str], Handler] = {}

    def bind(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.loop = loop

    # Thread-safe sending ------------------------------------------------------------------

    def send(self, node_id: str, message: dict) -> None:
        """Queue a JSON message for a connected Worker from any thread; dropped if it is not
        connected."""
        loop = self.loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._deliver, node_id, message)
        except RuntimeError:
            pass  # The loop is shutting down.

    def ring(self, node_id: str) -> None:
        """Tell a Worker that commands are waiting; called after the transaction commits."""
        self.send(node_id, {"type": "commands"})

    def _deliver(self, node_id: str, message: dict) -> None:
        peer = self.workers.get(node_id)
        if peer is not None:
            peer.queue.put_nowait(message)

    # Messages from Workers -----------------------------------------------------------------

    def on(self, kind: str, handler: Handler) -> None:
        """Handle Worker messages whose "type" is kind and whose "id" nobody subscribed to."""
        self.handlers[kind] = handler

    def subscribe(self, node_id: str, message_id: str, handler: Handler) -> Callable[[], None]:
        """Route messages carrying this "id" from this node's Worker to handler (e.g. one
        fast-lane command's output); returns the matching unsubscribe. Keyed by node too, so
        another Worker cannot inject into a session that targets a different node."""
        key = (node_id, message_id)
        self.subscriptions[key] = handler
        return lambda: self.subscriptions.pop(key, None)

    async def _dispatch(self, node_id: str, message) -> None:
        if not isinstance(message, dict):
            log.warning("dropped a non-object message from %s", node_id)
            return
        handler = self.subscriptions.get((node_id, message.get("id"))) or self.handlers.get(
            message.get("type")
        )
        if handler is None:
            log.debug("dropped an unhandled %r message from %s", message.get("type"), node_id)
            return
        try:
            result = handler(node_id, message)
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("hub handler failed for a %r message", message.get("type"))

    # Connection lifecycle ------------------------------------------------------------------

    async def serve_worker(self, node_id: str, instance_id: str, websocket: WebSocket) -> None:
        """Run an authenticated, accepted Worker connection until it closes."""
        peer = Peer(websocket, instance_id)
        previous = self.workers.get(node_id)
        self.workers[node_id] = peer
        if previous is not None:
            previous.queue.put_nowait(None)  # A newer connection replaces it.
        writer = asyncio.create_task(peer.write())
        reader = asyncio.create_task(self._read(node_id, websocket))
        try:
            await asyncio.wait({writer, reader}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if self.workers.get(node_id) is peer:
                del self.workers[node_id]
            for task in (writer, reader):
                task.cancel()
            # wait, not gather: a gather cancelled by the host's own cancellation (TestClient
            # teardown, server shutdown) could escape the host's cancel scope.
            await asyncio.wait({writer, reader})
            for task in (writer, reader):
                if not task.cancelled():
                    task.exception()  # Retrieved: a failed send just ends the connection.
            try:
                await websocket.close()
            except Exception:
                pass  # Already closed by the peer.

    async def _read(self, node_id: str, websocket: WebSocket) -> None:
        try:
            while True:
                try:
                    message = await websocket.receive_json()
                except ValueError:
                    log.warning("dropped a malformed message from %s", node_id)
                    continue
                await self._dispatch(node_id, message)
        except WebSocketDisconnect:
            pass
