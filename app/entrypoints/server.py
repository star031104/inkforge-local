"""Canonical HTTP server entrypoint for source, launcher, and packaged builds."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

import uvicorn

from ..core.config import settings
from ..core.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动砚火本地写作工作台")
    parser.add_argument("--host", default=os.environ.get("INKFORGE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("INKFORGE_PORT", "7860"))
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _open_when_ready(url: str, timeout_seconds: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    health_url = url.rstrip("/") + "/api/health"
    while time.monotonic() < deadline:
        try:
            with urlopen(health_url, timeout=1.0) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except (OSError, URLError):
            time.sleep(0.25)


def serve(*, host: str, port: int, open_browser: bool, verbose: bool = False) -> None:
    log_path = Path(
        os.environ.get(
            "INKFORGE_LOG_PATH",
            str(settings.database_path.parent / "logs" / "inkforge.log"),
        )
    ).resolve()
    configure_logging(log_path, verbose=verbose)
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="debug" if verbose else "info",
        log_config=None,
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    no_browser = args.no_browser or os.environ.get("INKFORGE_NO_BROWSER") == "1"
    serve(
        host=args.host,
        port=args.port,
        open_browser=not no_browser,
        verbose=args.verbose,
    )


def desktop_main() -> None:
    main()


if __name__ == "__main__":
    main()
