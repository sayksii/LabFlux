"""A CPU stand-in for an OpenAI-compatible model server (plan 10.2), standard library only.

It answers like vLLM: GET /health, GET /v1/models, and POST /v1/chat/completions,
/v1/completions and /v1/embeddings, with "stream": true sent as server-sent events. The reply
echoes the last message (or the prompt) one word per event. Settings, all optional:

  FAKE_MODEL_NAME     model id it answers to (default "fake")
  PORT                listening port (default 8000)
  FAKE_STARTUP_S      seconds before /health turns 200, like a model loading (default 0)
  FAKE_TOKEN_DELAY_S  pause between streamed words (default 0.05)
  FAKE_EXIT_AFTER_S   exit with status 3 after this many seconds, to test failures (default off)

Run it as a LabFlux model by mounting this directory as a dataset, for example
argv = ["python", "/datasets/fake-model/server.py"] with image python:3.12-slim.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = os.environ.get("FAKE_MODEL_NAME", "fake")
PORT = int(os.environ.get("PORT", "8000"))
STARTUP_S = float(os.environ.get("FAKE_STARTUP_S", "0"))
TOKEN_DELAY_S = float(os.environ.get("FAKE_TOKEN_DELAY_S", "0.05"))
EXIT_AFTER_S = os.environ.get("FAKE_EXIT_AFTER_S")
STARTED = time.monotonic()


def text_of(body: dict, path: str) -> str:
    if path == "/v1/chat/completions":
        messages = body.get("messages") or [{}]
        content = messages[-1].get("content", "")
        return content if isinstance(content, str) else json.dumps(content)
    prompt = body.get("prompt", "")
    return prompt if isinstance(prompt, str) else json.dumps(prompt)


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-model/1"

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's signature
        print(f"{self.address_string()} {format % args}", flush=True)

    def send_json(self, status: int, value: dict) -> None:
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - http.server naming
        if self.path == "/health":
            ready = time.monotonic() - STARTED >= STARTUP_S
            self.send_json(200 if ready else 503, {"ready": ready})
        elif self.path == "/v1/models":
            self.send_json(200, {"object": "list", "data": [{"id": NAME, "object": "model"}]})
        else:
            self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_json(400, {"error": {"message": "invalid JSON"}})
            return
        if body.get("model") != NAME:
            self.send_json(404, {"error": {"message": f"model {body.get('model')!r} not found"}})
            return
        created = int(time.time())
        if self.path == "/v1/embeddings":
            text = str(body.get("input", ""))
            vector = [((sum(map(ord, text)) * (i + 1)) % 997) / 997 for i in range(8)]
            self.send_json(
                200,
                {
                    "object": "list",
                    "model": NAME,
                    "data": [{"object": "embedding", "index": 0, "embedding": vector}],
                },
            )
            return
        if self.path not in ("/v1/chat/completions", "/v1/completions"):
            self.send_json(404, {"error": {"message": "not found"}})
            return
        chat = self.path == "/v1/chat/completions"
        words = ("echo: " + text_of(body, self.path)).split(" ")
        kind = "chat.completion" if chat else "text_completion"
        if not body.get("stream"):
            reply = " ".join(words)
            choice = (
                {"index": 0, "message": {"role": "assistant", "content": reply}}
                if chat
                else {"index": 0, "text": reply}
            )
            choice["finish_reason"] = "stop"
            self.send_json(
                200,
                {
                    "id": "fake",
                    "object": kind,
                    "created": created,
                    "model": NAME,
                    "choices": [choice],
                },
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()  # HTTP/1.0: the response ends when the connection closes.
        try:
            for i, word in enumerate(words):
                piece = word if i == 0 else " " + word
                delta = {"index": 0, "delta": {"content": piece}} if chat else {"index": 0}
                if not chat:
                    delta["text"] = piece
                event = {
                    "id": "fake",
                    "object": f"{kind}.chunk",
                    "created": created,
                    "model": NAME,
                    "choices": [delta],
                }
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                time.sleep(TOKEN_DELAY_S)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            print("client disconnected mid-stream", flush=True)


def main() -> None:
    if EXIT_AFTER_S:
        threading.Timer(float(EXIT_AFTER_S), lambda: os._exit(3)).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"fake model {NAME!r} listening on {PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
