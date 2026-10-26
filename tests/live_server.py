"""Live uvicorn server + barrier helper for concurrency tests (spec §14).

* The real app runs in a background thread on a free port, so requests go through
  the full HTTP stack, and each request gets its own DB session.
* Every worker thread has its OWN httpx.Client.
* A threading.Barrier releases all workers at the same instant, so requests really collide.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import httpx
import uvicorn
from fastapi import FastAPI

T = TypeVar("T")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    def __init__(self, app: FastAPI) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("live test server did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=60)


def run_concurrently(server: LiveServer, n: int, work: Callable[[int, httpx.Client], T]) -> list[T]:
    """Run `work(i, client)` in n threads, all released together by a barrier."""
    barrier = threading.Barrier(n)

    def worker(i: int) -> T:
        with server.client() as http:
            barrier.wait(timeout=30)
            return work(i, http)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(worker, i) for i in range(n)]
        return [f.result() for f in futures]
