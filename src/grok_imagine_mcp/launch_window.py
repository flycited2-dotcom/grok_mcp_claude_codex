"""Открывает окно Chrome для Grok (профиль и порт из ~/.grok_imagine_mcp/.env), если оно ещё не открыто.

Нужен для автозапуска после входа в Windows (планировщик задач): python -m grok_imagine_mcp.launch_window
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .chrome import ensure_chrome, find_chrome
from .drivers import DriverError


def main(environ=None, ensure=ensure_chrome) -> int:
    from .server import _state_dir, merged_env      # server тянет mcp: импортируем только при запуске
    environ = os.environ if environ is None else environ
    state = _state_dir()
    env = merged_env(state, environ)
    port = int(env.get("GROK_WEB_PORT") or 9340)
    profile = Path(env.get("GROK_CHROME_PROFILE") or state / "chrome-profile")
    try:
        reused = asyncio.run(ensure(port, profile, find_chrome(env)))
    except DriverError as e:
        print(f"окно Grok не открыто: {e}", file=sys.stderr)
        return 1
    print("окно уже было открыто" if reused else "окно Grok открыто")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
