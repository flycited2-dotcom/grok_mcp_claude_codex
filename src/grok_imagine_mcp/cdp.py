"""Минимальный клиент Chrome DevTools Protocol: команды, события и HTTP-эндпоинты порта отладки."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp


class CdpError(Exception):
    """Ошибка протокола CDP или страницы."""


class CdpConnectionLost(CdpError):
    """Связь с Chrome потеряна (окно закрыли, Chrome упал или перезапускается): задачу с post_id можно дожать позже."""


async def http_json(port: int, path: str, *, method: str = "GET", timeout: float = 2.0) -> Any | None:
    """Запрос к http://127.0.0.1:<port><path>: JSON ответа или None, если порт не отвечает."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.request(method, url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return None


class CdpConnection:
    def __init__(self, session: aiohttp.ClientSession, ws: aiohttp.ClientWebSocketResponse):
        self._session, self._ws = session, ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: list[dict] = []
        self._arrived = asyncio.Event()
        self._closed = False
        self._reader = asyncio.get_running_loop().create_task(self._read_loop())

    @classmethod
    async def connect(cls, ws_url: str) -> "CdpConnection":
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(ws_url, max_msg_size=0)       # base64 видео бывает десятки МБ
        except (aiohttp.ClientError, OSError) as e:
            await session.close()
            raise CdpConnectionLost(f"не удалось подключиться к Chrome: {e}") from e
        return cls(session, ws)

    async def _read_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data:
                    fut = self._pending.pop(data["id"], None)
                    if fut is None or fut.done():
                        continue
                    if "error" in data:
                        err = data["error"]
                        fut.set_exception(CdpError(f"{err.get('message', 'ошибка CDP')} (код {err.get('code')})"))
                    else:
                        fut.set_result(data.get("result", {}))
                elif "method" in data:
                    self._events.append(data)
                    self._arrived.set()
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CdpConnectionLost("соединение с Chrome закрыто"))
            self._pending.clear()
            self._arrived.set()

    async def send(self, method: str, **params) -> dict:
        if self._closed:
            raise CdpConnectionLost("соединение с Chrome закрыто")
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = fut
        await self._ws.send_str(json.dumps({"id": self._next_id, "method": method, "params": params}))
        return await fut

    async def wait_event(self, name: str, timeout: float = 5.0) -> dict | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for i, event in enumerate(self._events):
                if event["method"] == name:
                    del self._events[i]
                    return event.get("params", {})
            remaining = deadline - loop.time()
            if remaining <= 0 or self._closed:
                return None
            self._arrived.clear()          # между проверкой и clear() нет await, событие не потеряется
            try:
                await asyncio.wait_for(self._arrived.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    async def evaluate(self, expression: str) -> Any:
        r = await self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise CdpError(f"ошибка в странице: {(d.get('exception') or {}).get('description') or d.get('text', '')}")
        return r["result"].get("value")

    async def close(self) -> None:
        await self._ws.close()
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        await self._session.close()
