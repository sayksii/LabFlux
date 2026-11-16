#!/usr/bin/env python3
"""Send one request to the LabFlux inference Gateway (OpenAI-compatible /v1), standard library only.

The base URL and key come from OPENAI_BASE_URL and OPENAI_API_KEY; when they are not set, from
the file `labflux login` writes (~/.config/labflux/login.toml: scheduler_url + "/v1" and
user_token). Works with Python 3.8 or newer. Timing goes to stderr, so two copies started
together show that both requests are served at the same time.

  python3 examples/gateway/chat.py --list
  python3 examples/gateway/chat.py --model gemma4-31b "用一句話介紹你自己"
  python3 examples/gateway/chat.py --model gemma4-31b --stream "Count from 1 to 10."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOGIN_FILE = Path.home() / ".config" / "labflux" / "login.toml"
# The Gateway is reached directly or through an SSH tunnel, never through an HTTP proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_login(text: str) -> dict:
    """The two `key = "value"` lines `labflux login` writes. No tomllib: lab nodes may still
    have Python 3.10 as python3."""
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and value.strip().startswith('"'):
            values[key.strip()] = json.loads(value.strip())
    return values


def endpoint() -> tuple[str, str]:
    base, key = os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_API_KEY")
    if not (base and key):
        if not LOGIN_FILE.exists():
            sys.exit("Set OPENAI_BASE_URL and OPENAI_API_KEY, or run `labflux login` on a node.")
        login = read_login(LOGIN_FILE.read_text())
        base = base or login["scheduler_url"].rstrip("/") + "/v1"
        key = key or login["user_token"]
    return base.rstrip("/"), key


def request(base: str, key: str, path: str, body: dict | None, timeout: float):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return OPENER.open(urllib.request.Request(base + path, data, headers), timeout=timeout)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("prompt", nargs="?", default="Say hello in one short sentence.")
    parser.add_argument("--model", default="gemma4-31b")
    parser.add_argument("--list", action="store_true", help="list the models and their state")
    parser.add_argument("--stream", action="store_true", help="print the answer as it is generated")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=900, help="seconds; covers a cold start")
    options = parser.parse_args()
    base, key = endpoint()
    started = time.monotonic()
    try:
        if options.list:
            with request(base, key, "/models", None, 30) as response:
                for model in json.load(response)["data"]:
                    view = model.get("labflux", {})
                    shown = ("mode", "state", "node_id", "inflight")
                    print(model["id"], *(f"{k}={view.get(k)}" for k in shown))
            return
        body = {
            "model": options.model,
            "messages": [{"role": "user", "content": options.prompt}],
            "max_tokens": options.max_tokens,
            "stream": options.stream,
        }
        with request(base, key, "/chat/completions", body, options.timeout) as response:
            if not options.stream:
                reply = json.load(response)
                print(reply["choices"][0]["message"]["content"])
                print(
                    f"[{time.monotonic() - started:.2f} s, usage {reply.get('usage')}]",
                    file=sys.stderr,
                )
                return
            first = None
            for raw in response:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                first = first or time.monotonic()
                delta = json.loads(data)["choices"][0].get("delta", {})
                print(delta.get("content") or "", end="", flush=True)
            print(flush=True)
            print(
                f"[first chunk {first - started:.2f} s, done {time.monotonic() - started:.2f} s]"
                if first
                else "[no chunks received]",
                file=sys.stderr,
            )
    except urllib.error.HTTPError as error:
        # 503 with Retry-After: the model is starting (cold start) or waiting for GPUs.
        retry = error.headers.get("Retry-After")
        detail = error.read().decode(errors="replace")[:500]
        hint = f" (retry after {retry} s)" if retry else ""
        sys.exit(f"HTTP {error.code}{hint}: {detail}")


if __name__ == "__main__":
    main()
