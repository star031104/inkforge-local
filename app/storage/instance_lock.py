"""Cross-process lock preventing two writers from opening one project database."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class ApplicationAlreadyRunningError(RuntimeError):
    pass


class ApplicationInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        if self._stream is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise ApplicationAlreadyRunningError(
                "同一个作品库已经由另一个砚火进程打开。请切回已运行的窗口，"
                "或先关闭旧进程再重新启动。"
            ) from exc
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None

