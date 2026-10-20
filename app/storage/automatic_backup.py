"""Periodic SQLite backups independent of HTTP request paths."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
import threading
import time
from typing import Protocol


logger = logging.getLogger(__name__)


class BackupStore(Protocol):
    def backup(self, directory: Path, keep: int = 12, *, prefix: str = "inkforge") -> Path: ...


class AutomaticBackupService:
    """Creates a bounded backup on startup and periodically while the app runs."""

    def __init__(
        self,
        store_provider: Callable[[], BackupStore],
        directory_provider: Callable[[], Path],
        *,
        interval_seconds: float = 3600,
        keep: int = 24,
    ):
        self.store_provider = store_provider
        self.directory_provider = directory_provider
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.keep = max(2, int(keep))
        self._lock = threading.Lock()

    def _latest_timestamp(self, directory: Path) -> float:
        candidates = list(directory.glob("inkforge-auto-*.db")) if directory.is_dir() else []
        return max((item.stat().st_mtime for item in candidates), default=0.0)

    def run_once(self, *, now: float | None = None) -> Path | None:
        with self._lock:
            directory = self.directory_provider().resolve()
            timestamp = time.time() if now is None else now
            if timestamp - self._latest_timestamp(directory) < self.interval_seconds:
                return None
            path = self.store_provider().backup(
                directory, keep=self.keep, prefix="inkforge-auto"
            )
            logger.info("automatic backup created: %s", path.name)
            return path

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:
                logger.exception("automatic backup failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(60.0, self.interval_seconds))
            except TimeoutError:
                continue
