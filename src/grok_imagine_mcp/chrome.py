"""Окно Chrome для Grok: поиск chrome.exe, запуск с профилем и портом отладки. Пароли не вводит, окно не закрывает."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from .cdp import http_json
from .drivers import DriverError

GROK_URL = "https://grok.com/imagine"


# клиент MCP может запустить сервер с урезанным окружением (без ProgramFiles), а Chrome стоит там, где всегда
_DEFAULT_CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def find_chrome(env: Mapping[str, str], *, fallbacks=_DEFAULT_CHROME_PATHS) -> Path | None:
    explicit = env.get("GROK_CHROME_PATH")
    if explicit:                                     # явно указанный путь не заменяем догадкой
        path = Path(explicit)
        return path if path.is_file() else None
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        root = env.get(var)
        if root:
            candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                return candidate
    return next((c for c in fallbacks if c.is_file()), None)


def build_command(chrome: Path, port: int, profile: Path, url: str = GROK_URL) -> list[str]:
    return [
        str(chrome), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        # перекрытое другим окном окно Windows считает скрытым, и Grok перестаёт рисовать в нём меню:
        # генерации идут без присмотра, поэтому окно должно работать и под другими окнами
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling", "--disable-features=CalculateNativeWinOcclusion", f"--app={url}",
    ]


def _spawn(cmd: list[str]) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True}
    if sys.platform == "win32":
        # окно должно пережить процесс сервера
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


async def ensure_chrome(port: int, profile: Path, chrome_path: Path | None, *,
                        probe: Callable[[int], Awaitable[object]] | None = None,
                        spawn: Callable[[list[str]], None] | None = None,
                        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                        now: Callable[[], float] = time.monotonic, timeout_s: float = 30.0) -> bool:
    """True: окно уже было. False: запустили сейчас. Чужой процесс на порту не трогаем."""
    probe = probe or (lambda p: http_json(p, "/json/version"))
    if await probe(port):
        return True
    if chrome_path is None:
        raise DriverError("Chrome не найден. Установите Google Chrome или задайте GROK_CHROME_PATH "
                          "(полный путь к chrome.exe).")
    Path(profile).mkdir(parents=True, exist_ok=True)
    (spawn or _spawn)(build_command(chrome_path, port, Path(profile)))
    deadline = now() + timeout_s
    while now() < deadline:
        await sleep(0.5)
        if await probe(port):
            return False
    raise DriverError(f"Chrome запущен, но порт отладки {port} не ответил за {int(timeout_s)} с. "
                      "Возможно, порт занят другой программой: задайте другой GROK_WEB_PORT.")
