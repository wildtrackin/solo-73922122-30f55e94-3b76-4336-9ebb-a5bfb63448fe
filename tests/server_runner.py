"""Test-only server launcher for the two-process scenarios.

The sandbox used for these tests does not allow sending signals to child
processes, so the servers stop by watching a sentinel file instead.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import uvicorn

from app.main import app


def main() -> None:
    port = int(sys.argv[1])
    stop_file = sys.argv[2]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )

    def watch() -> None:
        deadline = time.time() + 600  # safety net for abandoned runs
        while time.time() < deadline and not os.path.exists(stop_file):
            time.sleep(0.1)
        server.should_exit = True

    threading.Thread(target=watch, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
