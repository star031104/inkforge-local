"""Launch the packaged executable on an isolated port and verify health."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    port = available_port()
    with tempfile.TemporaryDirectory(prefix="inkforge-packaged-") as directory:
        environment = os.environ.copy()
        environment.update(
            {
                "LOCALAPPDATA": directory,
                "INKFORGE_PORT": str(port),
                "INKFORGE_NO_BROWSER": "1",
            }
        )
        process = subprocess.Popen(
            [str(executable)],
            cwd=executable.parent,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = (process.stdout.read() if process.stdout else b"").decode(
                        "utf-8", errors="replace"
                    )
                    raise RuntimeError(f"packaged app exited early:\n{output}")
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    payload = json.loads(response.read().decode("utf-8"))
                    if response.status == 200 and payload.get("status") == "ok":
                        print(json.dumps(payload, ensure_ascii=False))
                        return 0
                except (OSError, ValueError):
                    time.sleep(0.3)
            raise RuntimeError("packaged app health check timed out")
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

