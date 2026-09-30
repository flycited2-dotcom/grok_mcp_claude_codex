"""Межпроцессная блокировка: окно Chrome у Claude Code и Codex общее, а процессов сервера два."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable


class FileLock:
    """Блокировка на открытом файле: ОС снимает её сама, если процесс упал."""

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            raise RuntimeError("блокировка уже захвачена этим объектом")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    async def acquire(self, timeout_s: float, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                      now: Callable[[], float] = time.monotonic, poll_s: float = 0.5) -> bool:
        deadline = now() + timeout_s
        while not self.try_acquire():
            if now() >= deadline:
                return False
            await sleep(poll_s)
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh.close()                      # на POSIX flock снимается закрытием файла
