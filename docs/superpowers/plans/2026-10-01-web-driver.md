# Драйвер `web` для grok-imagine-mcp — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в `grok-imagine-mcp` драйвер `web`, который генерирует видео и картинки Grok Imagine через окно Chrome (подписка, без `XAI_API_KEY`) и отдаёт файлы через те же пять MCP-инструментов.

**Architecture:** Новый драйвер встаёт рядом с `xai` и `dry-run` за интерфейсом `Driver`. Он работает с подставляемым `WebBackend` (в тестах фейк), а настоящий backend управляет страницей `grok.com/imagine` в отдельном окне Chrome через собственный минимальный клиент CDP. Задачи живут в существующем `TaskStore`, очередь между процессами (Claude Code и Codex) обеспечивает файловая блокировка, результат скачивается `fetch`-ом внутри страницы и проверяется на целостность.

**Tech Stack:** Python ≥ 3.11, `aiohttp` (WebSocket и HTTP), `pydantic` и `mcp` (уже есть), `pytest`, Chrome 154 с CDP.

**Spec:** `tools/grok_imagine_mcp/docs/superpowers/specs/2026-10-01-web-driver-design.md` (читать вместе с планом: в нём результаты живой пробы, то есть точные подписи элементов и механика страницы).

> **Статус: выполнен.** Фактическое поведение описано в спецификации; план остался историческим документом. Отличия от плана,
> найденные живыми прогонами и ревью всей ветки: режим «Изображение» (сетка `data:`-картинок без страницы поста), окно,
> закрытое другими окнами (`CalculateNativeWinOcclusion`), поиск Chrome при урезанном окружении, «сердцебиение» задач вместо
> порога очереди, замок в профиле Chrome, опознание окна по `--user-data-dir`, перечитывание выбранных параметров, потеря
> связи с окном (`CdpConnectionLost`).

## Global Constraints

- Python `>=3.11` (как в `pyproject.toml`). Новая прямая зависимость: `aiohttp>=3.9`.
- Статусы задач прежние: `pending → done | failed`. Драйвер `web` не использует `expired` и `recorded_dry_run`.
- Допустимые значения в интерфейсе Grok: `duration_s` ∈ {6, 10, 15}; `resolution` ∈ {480p, 720p, 1080p}; `aspect_ratio` ∈ {2:3, 3:2, 1:1, 9:16, 16:9}. Значения 4:3 и 3:4 отклоняются. Значения молча не подгоняются.
- Порт отладки по умолчанию `9340`, Chrome слушает `--remote-debugging-address=127.0.0.1`. Профиль по умолчанию `<GROK_STATE_DIR>\chrome-profile`.
- Переменные окружения: `GROK_DRIVER=web`, `GROK_WEB_PORT` (9340), `GROK_CHROME_PROFILE`, `GROK_CHROME_PATH`, `GROK_WEB_GENERATION_TIMEOUT_S` (900), `GROK_WEB_QUEUE_TIMEOUT_S` (900).
- Драйвер **никогда** не вводит логин, пароль, коды, не обходит капчу, не нажимает согласия и баннер cookie, не читает Библиотеку, историю и названия чатов.
- Окно Chrome сервер не закрывает, процессы Chrome не убивает.
- Тексты для пользователя и модели — по-русски; комментарии в коде — по-русски и только там, где объясняют «почему» (как в существующем коде).
- Тесты не используют реальный Chrome и реальный API. Живой тест помечен `live` и пропускается без `GROK_WEB_LIVE=1`.
- Команды `pytest` запускать из `tools\grok_imagine_mcp`: `.\.venv\Scripts\python.exe -m pytest ...`. Команды `git` запускать оттуда же (пути относительно этой папки).
- Коммиты: `тип(grok-imagine-mcp): описание`, вторым `-m` всегда строка `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. `git push` не выполнять.

## Review Focus

Входные условия, которых спецификация прямо не разбирает, но которые чаще всего сломают драйвер у живого человека (каждое закрыто тестом в указанной задаче):

1. **Два процесса сервера одновременно** (Claude Code и Codex ставят задачи на одно окно Chrome): второй не должен лезть в страницу, пока первый работает, и не должен портить его задачу. Тесты: Task 3 и Task 6.
2. **Сервер перезапущен посреди генерации**: задача с `post_id` дожимается, задача без `post_id` не висит вечно, а задача в очереди не объявляется мёртвой раньше времени. Тесты: Task 6.
3. **Скачался обрезанный или битый файл** (Grok отвечает HTTP 206, соединение может оборваться): задача не должна стать `done` с негодным mp4. Тесты: Task 2 и Task 7.
4. **Chrome не установлен или порт отладки занят чужой программой**: понятная ошибка, а не зависание и не убитый процесс. Тесты: Task 4.
5. **Профиль Chrome не вошёл в аккаунт** (сессия слетела): ответ «войдите вручную», без попыток логина; `grok_status` честно показывает состояние. Тесты: Task 7 и Task 8.

Дополнительно закрыт тестом промт с переводами строк (Task 7): они схлопываются в пробелы, иначе многострочный промт может отправиться раньше времени.

---

### Task 1: Клиент CDP (`cdp.py`)

**Files:**
- Create: `src/grok_imagine_mcp/cdp.py`
- Create: `tests/test_cdp.py`
- Modify: `pyproject.toml` (зависимость `aiohttp>=3.9`)

**Interfaces:**
- Produces:
  - `class CdpError(Exception)`
  - `async def http_json(port: int, path: str, *, method: str = "GET", timeout: float = 2.0) -> Any | None` — JSON ответа или `None`, если порт не отвечает или статус не 200.
  - `class CdpConnection`:
    - `@classmethod async def connect(cls, ws_url: str) -> CdpConnection`
    - `async def send(self, method: str, **params) -> dict` — результат команды; `CdpError` при ошибке протокола или закрытом соединении.
    - `async def wait_event(self, name: str, timeout: float = 5.0) -> dict | None` — `params` первого события с таким именем (событие извлекается из очереди), `None` по таймауту.
    - `async def evaluate(self, expression: str) -> Any` — `Runtime.evaluate` с `returnByValue` и `awaitPromise`; `CdpError`, если в странице выброшено исключение.
    - `async def close(self) -> None`

- [ ] **Step 1: Write the failing test**

Создать `tests/test_cdp.py`:

```python
import asyncio
import json

import pytest
from aiohttp import web

from grok_imagine_mcp.cdp import CdpConnection, CdpError, http_json


async def _ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        d = json.loads(msg.data)
        method, i = d["method"], d["id"]
        if method == "Echo":
            await ws.send_str(json.dumps({"id": i, "result": {"params": d["params"]}}))
        elif method == "Boom":
            await ws.send_str(json.dumps({"id": i, "error": {"code": -32000, "message": "нет такого узла"}}))
        elif method == "WithEvent":
            # событие приходит раньше ответа: клиент не должен его потерять
            await ws.send_str(json.dumps({"method": "Page.fileChooserOpened", "params": {"backendNodeId": 7}}))
            await ws.send_str(json.dumps({"id": i, "result": {}}))
        elif method == "Runtime.evaluate":
            if d["params"]["expression"] == "1+1":
                result = {"result": {"type": "number", "value": 2}}
            else:
                result = {"result": {"type": "object"},
                          "exceptionDetails": {"text": "Uncaught",
                                               "exception": {"description": "ReferenceError: x is not defined"}}}
            await ws.send_str(json.dumps({"id": i, "result": result}))
        elif method == "Hang":
            await ws.close()
    return ws


async def _version(request):
    return web.json_response({"Browser": "Chrome/154"})


async def _serve():
    app = web.Application()
    app.router.add_get("/ws", _ws_handler)
    app.router.add_get("/json/version", _version)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, runner.addresses[0][1]


def _run(body):
    async def main():
        runner, port = await _serve()
        conn = await CdpConnection.connect(f"ws://127.0.0.1:{port}/ws")
        try:
            return await body(conn)
        finally:
            await conn.close()
            await runner.cleanup()
    return asyncio.run(main())


def test_send_returns_result():
    async def body(conn):
        return await conn.send("Echo", a=1)
    assert _run(body) == {"params": {"a": 1}}


def test_protocol_error_becomes_cdp_error():
    async def body(conn):
        with pytest.raises(CdpError, match="нет такого узла"):
            await conn.send("Boom")
    _run(body)


def test_event_arriving_before_the_response_is_kept():
    async def body(conn):
        await conn.send("WithEvent")
        first = await conn.wait_event("Page.fileChooserOpened", timeout=1)
        second = await conn.wait_event("Page.fileChooserOpened", timeout=0.1)
        return first, second
    assert _run(body) == ({"backendNodeId": 7}, None)


def test_wait_event_times_out_with_none():
    async def body(conn):
        return await conn.wait_event("Page.loadEventFired", timeout=0.1)
    assert _run(body) is None


def test_evaluate_returns_value_and_raises_page_exceptions():
    async def body(conn):
        value = await conn.evaluate("1+1")
        with pytest.raises(CdpError, match="ReferenceError"):
            await conn.evaluate("x")
        return value
    assert _run(body) == 2


def test_closed_connection_fails_pending_command():
    async def body(conn):
        with pytest.raises(CdpError, match="закрыто"):
            await conn.send("Hang")
    _run(body)


def test_http_json_reads_the_endpoint_and_returns_none_when_port_is_closed():
    async def main():
        runner, port = await _serve()
        try:
            ok = await http_json(port, "/json/version")
        finally:
            await runner.cleanup()
        gone = await http_json(port, "/json/version", timeout=0.5)
        return ok, gone
    ok, gone = asyncio.run(main())
    assert ok == {"Browser": "Chrome/154"} and gone is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cdp.py -q`
Expected: FAIL (ошибка сбора: `ModuleNotFoundError: No module named 'grok_imagine_mcp.cdp'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/cdp.py`:

```python
"""Минимальный клиент Chrome DevTools Protocol: команды, события и HTTP-эндпоинты порта отладки."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp


class CdpError(Exception):
    """Ошибка протокола CDP или потеря соединения с Chrome."""


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
            raise CdpError(f"не удалось подключиться к Chrome: {e}") from e
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
                    fut.set_exception(CdpError("соединение с Chrome закрыто"))
            self._pending.clear()
            self._arrived.set()

    async def send(self, method: str, **params) -> dict:
        if self._closed:
            raise CdpError("соединение с Chrome закрыто")
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
```

В `pyproject.toml` добавить зависимость в `[project] dependencies` (рядом с `httpx`):

```toml
  "aiohttp>=3.9",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_cdp.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/cdp.py tests/test_cdp.py pyproject.toml
git commit -m "feat(grok-imagine-mcp): клиент CDP для драйвера web" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Проверка скачанных файлов (`media_check.py`)

**Files:**
- Create: `src/grok_imagine_mcp/media_check.py`
- Create: `tests/test_media_check.py`

**Interfaces:**
- Produces:
  - `class MediaError(ValueError)`
  - `def check_mp4(data: bytes) -> None` — `MediaError`, если файл обрезан, блоки не сходятся с размером или нет `ftyp`/`moov`/`mdat`.
  - `def detect_image_ext(data: bytes) -> str` — `".png"`, `".jpg"` или `".webp"`; иначе `MediaError`.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_media_check.py`:

```python
import struct

import pytest

from grok_imagine_mcp.media_check import MediaError, check_mp4, detect_image_ext


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


GOOD = box(b"ftyp", b"isom0000") + box(b"moov", b"m" * 20) + box(b"mdat", b"d" * 100)


def test_whole_mp4_passes():
    check_mp4(GOOD)


def test_truncated_mp4_is_rejected():
    with pytest.raises(MediaError, match="обрезан"):
        check_mp4(GOOD[:-3])


def test_trailing_garbage_shorter_than_a_header_is_rejected():
    with pytest.raises(MediaError, match="обрезан"):
        check_mp4(GOOD + b"\x00\x00")


def test_missing_moov_is_rejected():
    with pytest.raises(MediaError, match="moov"):
        check_mp4(box(b"ftyp", b"isom0000") + box(b"mdat", b"d" * 10))


def test_empty_and_html_error_page_are_rejected():
    with pytest.raises(MediaError):
        check_mp4(b"")
    with pytest.raises(MediaError):
        check_mp4(b"<html><body>403 Forbidden</body></html>")


def test_last_box_with_zero_size_runs_to_the_end():
    data = box(b"ftyp", b"isom0000") + box(b"moov", b"m" * 4) + struct.pack(">I4s", 0, b"mdat") + b"d" * 50
    check_mp4(data)


def test_image_types_are_detected_by_content():
    assert detect_image_ext(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == ".png"
    assert detect_image_ext(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == ".jpg"
    assert detect_image_ext(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == ".webp"


def test_non_image_is_rejected():
    with pytest.raises(MediaError, match="не похож на картинку"):
        detect_image_ext(b"<html>")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_media_check.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'grok_imagine_mcp.media_check'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/media_check.py`:

```python
"""Проверки скачанных файлов: целый ли mp4 и какого типа картинка."""
from __future__ import annotations

import struct


class MediaError(ValueError):
    """Файл не прошёл проверку; текст показывается пользователю."""


def check_mp4(data: bytes) -> None:
    """Блоки верхнего уровня должны в сумме давать размер файла и включать ftyp, moov и mdat."""
    pos, kinds = 0, []
    while pos < len(data):
        if pos + 8 > len(data):
            raise MediaError("файл обрезан: хвост короче заголовка блока")
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if size == 1:                               # 64-битный размер
            if pos + 16 > len(data):
                raise MediaError("файл обрезан: неполный расширенный заголовок блока")
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        elif size == 0:                             # блок тянется до конца файла
            size = len(data) - pos
        if size < 8:
            raise MediaError("битый заголовок блока")
        kinds.append(kind)
        pos += size
    if pos != len(data):
        raise MediaError(f"файл обрезан: блоки занимают {pos} байт, а файл {len(data)}")
    missing = [k.decode() for k in (b"ftyp", b"moov", b"mdat") if k not in kinds]
    if missing:
        raise MediaError("в файле нет блоков: " + ", ".join(missing))


def detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise MediaError("скачанный файл не похож на картинку PNG/JPG/WEBP")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_media_check.py -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/media_check.py tests/test_media_check.py
git commit -m "feat(grok-imagine-mcp): проверка целостности скачанного mp4 и типа картинки" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Межпроцессная блокировка (`web_lock.py`)

**Files:**
- Create: `src/grok_imagine_mcp/web_lock.py`
- Create: `tests/test_web_lock.py`

**Interfaces:**
- Produces: `class FileLock`:
  - `FileLock(path)`
  - `def try_acquire(self) -> bool` — `True`, если захватила; `RuntimeError`, если этот же объект уже держит блокировку.
  - `async def acquire(self, timeout_s: float, *, sleep=asyncio.sleep, now=time.monotonic, poll_s: float = 0.5) -> bool` — `False` по таймауту.
  - `def release(self) -> None` — безопасна без захвата и повторно.
  - На каждый исполнитель создаётся **свой** объект `FileLock`: блокировка принадлежит объекту, а не пути.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_web_lock.py`:

```python
import asyncio

import pytest

from grok_imagine_mcp.web_lock import FileLock


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def test_second_lock_on_same_file_is_refused_until_release(tmp_path):
    path = tmp_path / "deep" / "web.lock"
    a, b = FileLock(path), FileLock(path)
    assert a.try_acquire() is True
    assert b.try_acquire() is False
    a.release()
    assert b.try_acquire() is True
    b.release()


def test_same_object_cannot_acquire_twice(tmp_path):
    a = FileLock(tmp_path / "web.lock")
    assert a.try_acquire()
    with pytest.raises(RuntimeError):
        a.try_acquire()
    a.release()


def test_release_is_safe_without_acquire_and_twice(tmp_path):
    a = FileLock(tmp_path / "web.lock")
    a.release()
    assert a.try_acquire()
    a.release()
    a.release()


def test_acquire_times_out_while_the_lock_is_held(tmp_path):
    a, b, clock = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock"), Clock()
    assert a.try_acquire()
    assert asyncio.run(b.acquire(2.0, sleep=clock.sleep, now=clock.now)) is False
    a.release()


def test_acquire_succeeds_as_soon_as_the_holder_releases(tmp_path):
    a, b, clock = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock"), Clock()
    assert a.try_acquire()

    async def sleep_and_release(s):
        clock.t += s
        a.release()

    assert asyncio.run(b.acquire(5.0, sleep=sleep_and_release, now=clock.now)) is True
    b.release()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_web_lock.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'grok_imagine_mcp.web_lock'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/web_lock.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_web_lock.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/web_lock.py tests/test_web_lock.py
git commit -m "feat(grok-imagine-mcp): межпроцессная блокировка для очереди к окну Chrome" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Окно Chrome (`chrome.py`)

**Files:**
- Create: `src/grok_imagine_mcp/chrome.py`
- Create: `tests/test_chrome.py`

**Interfaces:**
- Consumes: `http_json` из Task 1; `DriverError` из `drivers.py`.
- Produces:
  - `GROK_URL = "https://grok.com/imagine"`
  - `def find_chrome(env: Mapping[str, str]) -> Path | None`
  - `def build_command(chrome: Path, port: int, profile: Path, url: str = GROK_URL) -> list[str]`
  - `async def ensure_chrome(port: int, profile: Path, chrome_path: Path | None, *, probe=None, spawn=None, sleep=asyncio.sleep, now=time.monotonic, timeout_s: float = 30.0) -> bool` — `True`, если окно уже было; `False`, если запущено сейчас; `DriverError` — Chrome не найден или порт не ожил. `probe(port)` — awaitable, вернёт данные `/json/version` или `None`.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_chrome.py`:

```python
import asyncio

import pytest

from grok_imagine_mcp.chrome import build_command, ensure_chrome, find_chrome
from grok_imagine_mcp.drivers import DriverError


def _exe(root, *parts):
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


def test_find_chrome_prefers_explicit_path(tmp_path):
    explicit = _exe(tmp_path, "custom", "chrome.exe")
    assert find_chrome({"GROK_CHROME_PATH": str(explicit)}) == explicit


def test_find_chrome_explicit_but_missing_is_none_not_a_guess(tmp_path):
    _exe(tmp_path, "pf", "Google", "Chrome", "Application", "chrome.exe")
    assert find_chrome({"GROK_CHROME_PATH": str(tmp_path / "нет.exe"), "ProgramFiles": str(tmp_path / "pf")}) is None


def test_find_chrome_looks_in_standard_places(tmp_path):
    found = _exe(tmp_path, "pf86", "Google", "Chrome", "Application", "chrome.exe")
    assert find_chrome({"ProgramFiles": str(tmp_path / "pf"), "ProgramFiles(x86)": str(tmp_path / "pf86")}) == found


def test_find_chrome_none_when_not_installed(tmp_path):
    assert find_chrome({"ProgramFiles": str(tmp_path)}) is None


def test_command_has_port_profile_app_window_and_no_headless(tmp_path):
    cmd = build_command(tmp_path / "chrome.exe", 9340, tmp_path / "prof")
    assert cmd[0] == str(tmp_path / "chrome.exe")
    assert "--remote-debugging-port=9340" in cmd
    assert "--remote-debugging-address=127.0.0.1" in cmd
    assert f"--user-data-dir={tmp_path / 'prof'}" in cmd
    assert "--app=https://grok.com/imagine" in cmd
    assert "--disable-background-timer-throttling" in cmd
    assert not any("headless" in a for a in cmd)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def test_existing_window_is_reused_and_nothing_is_spawned(tmp_path):
    spawned = []

    async def probe(port):
        return {"Browser": "Chrome/154"}

    reused = asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, spawn=spawned.append))
    assert reused is True and spawned == []


def test_missing_window_is_launched_and_waited_for(tmp_path):
    spawned, answers = [], [None, None, {"Browser": "Chrome/154"}]
    clock = Clock()

    async def probe(port):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    chrome = _exe(tmp_path, "chrome.exe")
    reused = asyncio.run(ensure_chrome(9340, tmp_path / "prof", chrome, probe=probe, spawn=spawned.append,
                                       sleep=clock.sleep, now=clock.now))
    assert reused is False
    assert len(spawned) == 1 and "--remote-debugging-port=9340" in spawned[0]
    assert (tmp_path / "prof").is_dir()


def test_chrome_not_installed_is_a_clear_error(tmp_path):
    async def probe(port):
        return None

    with pytest.raises(DriverError, match="GROK_CHROME_PATH"):
        asyncio.run(ensure_chrome(9340, tmp_path / "p", None, probe=probe, spawn=lambda c: None))


def test_port_that_never_answers_points_to_port_setting(tmp_path):
    clock = Clock()

    async def probe(port):
        return None

    with pytest.raises(DriverError, match="GROK_WEB_PORT"):
        asyncio.run(ensure_chrome(9340, tmp_path / "p", tmp_path / "chrome.exe", probe=probe, spawn=lambda c: None,
                                  sleep=clock.sleep, now=clock.now, timeout_s=5))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_chrome.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'grok_imagine_mcp.chrome'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/chrome.py`:

```python
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


def find_chrome(env: Mapping[str, str]) -> Path | None:
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
    return None


def build_command(chrome: Path, port: int, profile: Path, url: str = GROK_URL) -> list[str]:
    return [
        str(chrome), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        # свёрнутое или перекрытое окно не должно замедлять страницу: генерации идут без присмотра
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling", f"--app={url}",
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_chrome.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/chrome.py tests/test_chrome.py
git commit -m "feat(grok-imagine-mcp): запуск и подключение к окну Chrome для Grok" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Общие типы и сопоставление параметров (`web_types.py`, `web_options.py`)

**Files:**
- Create: `src/grok_imagine_mcp/web_options.py`
- Create: `src/grok_imagine_mcp/web_types.py`
- Create: `tests/test_web_options.py`

**Interfaces:**
- Consumes: `VideoRequest`, `ImageRequest` из `models.py`; `DriverError` из `drivers.py`.
- Produces (`web_options.py`):
  - `WEB_DURATIONS = (6, 10, 15)`, `WEB_ASPECTS = ("2:3", "3:2", "1:1", "9:16", "16:9")`
  - `@dataclass(frozen=True) VideoOptions(prompt: str, image_path: Path | None, resolution: str | None, duration_s: int | None, aspect_ratio: str | None, generate_audio: bool | None)`
  - `@dataclass(frozen=True) ImageOptions(prompt: str, aspect_ratio: str | None)`
  - `def video_options(req: VideoRequest) -> tuple[VideoOptions, list[str]]` — вторым значением заметки для пользователя; `DriverError` при недопустимых значениях.
  - `def image_options(req: ImageRequest) -> tuple[ImageOptions, list[str]]`
- Produces (`web_types.py`): `BackendState(chrome_up: bool, logged_in: bool | None, profile: str = "")`, `Generated(data: bytes, ext: str)`, `Reporter` (Protocol: `phase(name: str)`, `submitted(post_id: str)`), `WebBackend` (Protocol: `async state() -> BackendState`, `async generate(opts, reporter) -> Generated`, `async resume(post_id: str, kind: str, reporter) -> Generated`).

- [ ] **Step 1: Write the failing test**

Создать `tests/test_web_options.py`:

```python
import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.models import ImageRequest, VideoRequest
from grok_imagine_mcp.web_options import image_options, video_options

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_plain_video_request_passes_through_without_notes():
    opts, notes = video_options(VideoRequest(prompt="лодка", duration_s=10, resolution="720p",
                                             aspect_ratio="16:9", generate_audio=False))
    assert (opts.prompt, opts.duration_s, opts.resolution, opts.aspect_ratio, opts.generate_audio) == \
        ("лодка", 10, "720p", "16:9", False)
    assert opts.image_path is None and notes == []


@pytest.mark.parametrize("seconds", [1, 5, 8, 12])
def test_duration_outside_the_ui_choices_is_refused_with_the_list(seconds):
    with pytest.raises(DriverError, match="6, 10, 15"):
        video_options(VideoRequest(prompt="x", duration_s=seconds))


@pytest.mark.parametrize("ratio", ["4:3", "3:4"])
def test_aspect_the_ui_does_not_have_is_refused(ratio):
    with pytest.raises(DriverError, match="2:3, 3:2, 1:1, 9:16, 16:9"):
        video_options(VideoRequest(prompt="x", aspect_ratio=ratio))


def test_with_a_photo_the_aspect_is_ignored_and_the_user_is_told(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    opts, notes = video_options(VideoRequest(prompt="оживи", image_path=str(img), aspect_ratio="16:9"))
    assert opts.image_path == img and opts.aspect_ratio is None
    assert any("aspect_ratio проигнорирован" in n for n in notes)


def test_unsupported_aspect_is_refused_even_with_a_photo(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    with pytest.raises(DriverError, match="aspect_ratio"):
        video_options(VideoRequest(prompt="x", image_path=str(img), aspect_ratio="4:3"))


def test_non_default_model_is_noted_not_silently_dropped():
    _, notes = video_options(VideoRequest(prompt="x", model="grok-imagine-video-1.5"))
    assert any("model" in n and "не применяется" in n for n in notes)


def test_image_request_keeps_aspect_and_notes_ignored_fields():
    opts, notes = image_options(ImageRequest(prompt="кот", aspect_ratio="1:1", resolution="2k",
                                             model="grok-imagine-image-quality"))
    assert opts.prompt == "кот" and opts.aspect_ratio == "1:1"
    assert any("resolution" in n for n in notes) and any("model" in n for n in notes)


def test_image_aspect_outside_the_ui_choices_is_refused():
    with pytest.raises(DriverError, match="aspect_ratio"):
        image_options(ImageRequest(prompt="x", aspect_ratio="19.5:9"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_web_options.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'grok_imagine_mcp.web_options'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/web_options.py`:

```python
"""Что из запроса умеет интерфейс Grok. Неподдерживаемое отклоняется, значения не подгоняются."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .drivers import DriverError
from .models import ImageRequest, VideoRequest

WEB_DURATIONS = (6, 10, 15)
WEB_ASPECTS = ("2:3", "3:2", "1:1", "9:16", "16:9")
_DEFAULT_VIDEO_MODEL = "grok-imagine-video"
_DEFAULT_IMAGE_MODEL = "grok-imagine-image"


@dataclass(frozen=True)
class VideoOptions:
    prompt: str
    image_path: Path | None
    resolution: str | None
    duration_s: int | None
    aspect_ratio: str | None
    generate_audio: bool | None


@dataclass(frozen=True)
class ImageOptions:
    prompt: str
    aspect_ratio: str | None


def _refuse(name: str, value, allowed) -> DriverError:
    return DriverError(f"web-драйвер: {name}={value} не поддерживается интерфейсом Grok. "
                       f"Допустимо: {', '.join(str(a) for a in allowed)}.")


def video_options(req: VideoRequest) -> tuple[VideoOptions, list[str]]:
    notes: list[str] = []
    if req.duration_s is not None and req.duration_s not in WEB_DURATIONS:
        raise _refuse("duration_s", req.duration_s, WEB_DURATIONS)
    if req.aspect_ratio is not None and req.aspect_ratio not in WEB_ASPECTS:
        raise _refuse("aspect_ratio", req.aspect_ratio, WEB_ASPECTS)
    aspect = req.aspect_ratio
    if req.image_path is not None and aspect is not None:
        notes.append("aspect_ratio проигнорирован: с фото Grok берёт формат кадра из фото")
        aspect = None
    if req.model != _DEFAULT_VIDEO_MODEL:
        notes.append(f"model={req.model} не применяется: web-драйвер использует модель, выбранную в интерфейсе Grok")
    return VideoOptions(req.prompt, req.image_path, req.resolution, req.duration_s, aspect, req.generate_audio), notes


def image_options(req: ImageRequest) -> tuple[ImageOptions, list[str]]:
    notes: list[str] = []
    if req.aspect_ratio is not None and req.aspect_ratio not in WEB_ASPECTS:
        raise _refuse("aspect_ratio", req.aspect_ratio, WEB_ASPECTS)
    if req.resolution is not None:
        notes.append("resolution не применяется: web-драйвер не задаёт размер картинки")
    if req.model != _DEFAULT_IMAGE_MODEL:
        notes.append(f"model={req.model} не применяется: web-драйвер использует модель, выбранную в интерфейсе Grok")
    return ImageOptions(req.prompt, req.aspect_ratio), notes
```

Создать `src/grok_imagine_mcp/web_types.py`:

```python
"""Общие типы драйвера web: так driver и backend не импортируют друг друга."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .web_options import ImageOptions, VideoOptions


@dataclass(frozen=True)
class BackendState:
    chrome_up: bool
    logged_in: bool | None          # None: неизвестно (окна нет или страница не определена)
    profile: str = ""


@dataclass(frozen=True)
class Generated:
    data: bytes
    ext: str                        # ".mp4", ".png", ".jpg" или ".webp"


class Reporter(Protocol):
    def phase(self, name: str) -> None: ...

    def submitted(self, post_id: str) -> None: ...


class WebBackend(Protocol):
    async def state(self) -> BackendState: ...

    async def generate(self, opts: VideoOptions | ImageOptions, reporter: Reporter) -> Generated: ...

    async def resume(self, post_id: str, kind: str, reporter: Reporter) -> Generated: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_web_options.py -q`
Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/web_options.py src/grok_imagine_mcp/web_types.py tests/test_web_options.py
git commit -m "feat(grok-imagine-mcp): сопоставление параметров запроса с интерфейсом Grok и общие типы web" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Драйвер `WebDriver` (`web_driver.py`) и тексты сообщений

**Files:**
- Create: `src/grok_imagine_mcp/web_driver.py`
- Modify: `src/grok_imagine_mcp/drivers.py` (`describe()`: фазы и заметки для драйвера `web`)
- Create: `tests/test_web_driver.py`

**Interfaces:**
- Consumes: `WebBackend`, `BackendState`, `Generated`, `Reporter` (Task 5); `FileLock` (Task 3); `video_options`, `image_options` (Task 5); `TaskStore`; из `drivers.py`: `DriverError`, `load_task`, `now_iso`, `to_info`.
- Produces:
  - `class WebDriver` с `name = "web"`:
    - `WebDriver(backend, store: TaskStore, output_dir: Path, lock_path: Path, *, queue_timeout_s: float = 900.0, generation_timeout_s: float = 900.0, sleep=asyncio.sleep, now=time.monotonic, utcnow=<UTC now>)`
    - `async submit_video(req: VideoRequest) -> TaskInfo`, `async submit_image(req: ImageRequest) -> TaskInfo`, `async task_status(task_id: str, wait_s: int) -> TaskInfo`, `async list_tasks(limit: int) -> list[TaskInfo]`, `async status() -> DriverStatus`
  - Запись задачи: `task_id, kind, driver="web", status, phase, submitted, created_at, request, notes`, после отправки `post_id`, после завершения `output_path`/`error`.
  - Константы: `POLL_INTERVAL_S = 1.0`, `ORPHAN_GRACE_S = 300.0`.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_web_driver.py`:

```python
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grok_imagine_mcp.drivers import DriverError, now_iso
from grok_imagine_mcp.models import ImageRequest, VideoRequest
from grok_imagine_mcp.store import TaskStore
from grok_imagine_mcp.web_driver import WebDriver
from grok_imagine_mcp.web_lock import FileLock
from grok_imagine_mcp.web_types import BackendState, Generated

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
POST = "11111111-2222-3333-4444-555555555555"


class FakeBackend:
    def __init__(self, state=None, result=None):
        self._state = state or BackendState(chrome_up=True, logged_in=True, profile="P")
        self.result = result or Generated(b"VIDEO", ".mp4")
        self.calls = []
        self.exc = None
        self.gate = None                 # asyncio.Event: пока не выставлен, генерация «идёт»

    async def state(self):
        return self._state

    async def generate(self, opts, reporter):
        self.calls.append(("generate", opts))
        reporter.submitted(POST)
        if self.gate is not None:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        reporter.phase("downloading")
        return self.result

    async def resume(self, post_id, kind, reporter):
        self.calls.append(("resume", post_id, kind))
        if self.gate is not None:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        return self.result


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s
        await asyncio.sleep(0.01)        # дать исполнителю поработать


def make(tmp_path, backend=None, **kw):
    backend = backend or FakeBackend()
    clock = Clock()
    drv = WebDriver(backend, TaskStore(tmp_path / "tasks"), tmp_path / "out", tmp_path / "web.lock",
                    sleep=clock.sleep, now=clock.now, **kw)
    return drv, backend


def run(coro):
    return asyncio.run(coro)


def pending_task(store, **extra):
    task = {"task_id": store.new_id(), "kind": "video", "driver": "web", "submitted": True, "status": "pending",
            "phase": "generating", "created_at": now_iso(), "request": {"prompt": "x"}, "notes": [], **extra}
    store.save(task)
    return task


def test_video_is_queued_then_done_with_a_file(tmp_path):
    async def main():
        drv, backend = make(tmp_path)
        info = await drv.submit_video(VideoRequest(prompt="лодка", duration_s=6))
        done = await drv.task_status(info.task_id, 30)
        return info, done, backend
    info, done, backend = run(main())
    assert info.status == "pending" and info.submitted is False and "очереди" in info.message
    assert done.status == "done" and done.submitted is True
    out = Path(done.output_path)
    assert out.read_bytes() == b"VIDEO" and out.suffix == ".mp4" and out.parent == tmp_path / "out"
    saved = json.loads((tmp_path / "tasks" / f"{info.task_id}.json").read_text(encoding="utf-8"))
    assert saved["post_id"] == POST and "phase" not in saved and saved["driver"] == "web"
    kind, opts = backend.calls[0]
    assert kind == "generate" and opts.prompt == "лодка" and opts.duration_s == 6


def test_image_task_gets_the_extension_of_the_generated_file(tmp_path):
    async def main():
        drv, backend = make(tmp_path, FakeBackend(result=Generated(PNG, ".png")))
        info = await drv.submit_image(ImageRequest(prompt="кот", aspect_ratio="1:1"))
        return await drv.task_status(info.task_id, 30)
    done = run(main())
    assert done.kind == "image" and done.status == "done" and done.output_path.endswith(".png")


def test_unsupported_value_is_refused_and_no_task_is_created(tmp_path):
    async def main():
        drv, _ = make(tmp_path)
        with pytest.raises(DriverError, match="6, 10, 15"):
            await drv.submit_video(VideoRequest(prompt="x", duration_s=8))
    run(main())
    assert not list((tmp_path / "tasks").glob("*.json"))


def test_notes_reach_the_message(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)

    async def main():
        drv, _ = make(tmp_path)
        return await drv.submit_video(VideoRequest(prompt="оживи", image_path=str(img), aspect_ratio="16:9"))
    assert "aspect_ratio проигнорирован" in run(main()).message


def test_driver_error_from_backend_marks_task_failed_with_its_text(tmp_path):
    async def main():
        backend = FakeBackend()
        backend.exc = DriverError("Grok просит войти в аккаунт")
        drv, _ = make(tmp_path, backend)
        info = await drv.submit_video(VideoRequest(prompt="x"))
        return await drv.task_status(info.task_id, 30)
    done = run(main())
    assert done.status == "failed" and done.error == "Grok просит войти в аккаунт"
    assert done.output_path is None and "Генерация не удалась" in done.message


def test_unexpected_exception_is_reported_as_failed_not_lost(tmp_path):
    async def main():
        backend = FakeBackend()
        backend.exc = RuntimeError("boom")
        drv, _ = make(tmp_path, backend)
        info = await drv.submit_video(VideoRequest(prompt="x"))
        return await drv.task_status(info.task_id, 30)
    assert run(main()).error == "RuntimeError: boom"


def test_generation_timeout_fails_the_task_and_frees_the_lock(tmp_path):
    async def main():
        backend = FakeBackend()
        backend.gate = asyncio.Event()                     # генерация «зависла»
        drv, _ = make(tmp_path, backend, generation_timeout_s=0.05)
        info = await drv.submit_video(VideoRequest(prompt="x"))
        return await drv.task_status(info.task_id, 30)
    done = run(main())
    assert done.status == "failed" and "не завершил генерацию" in done.error
    lock = FileLock(tmp_path / "web.lock")
    assert lock.try_acquire()
    lock.release()


def test_second_driver_waits_for_the_first_through_the_lock(tmp_path):
    async def main():
        d1, b1 = make(tmp_path, queue_timeout_s=1e9)
        d2, b2 = make(tmp_path, queue_timeout_s=1e9)
        b1.gate = asyncio.Event()
        t1 = await d1.submit_video(VideoRequest(prompt="первая"))
        await asyncio.sleep(0.1)
        t2 = await d2.submit_video(VideoRequest(prompt="вторая"))
        await asyncio.sleep(0.2)
        while_first_runs = (len(b1.calls), len(b2.calls))
        b1.gate.set()
        r1 = await d1.task_status(t1.task_id, 30)
        r2 = await d2.task_status(t2.task_id, 30)
        return while_first_runs, r1, r2
    while_first_runs, r1, r2 = run(main())
    assert while_first_runs == (1, 0)
    assert r1.status == "done" and r2.status == "done"


def test_pending_task_with_post_id_is_resumed_after_a_restart(tmp_path):
    async def main():
        drv, backend = make(tmp_path)
        task = pending_task(drv.store, post_id=POST)
        done = await drv.task_status(task["task_id"], 30)
        return done, backend
    done, backend = run(main())
    assert done.status == "done" and Path(done.output_path).read_bytes() == b"VIDEO"
    assert backend.calls == [("resume", POST, "video")]


def test_task_without_post_id_is_failed_only_after_the_queue_limit_plus_grace(tmp_path):
    created = "2026-10-01T10:00:00+00:00"

    async def main(minutes_later):
        now = datetime(2026, 10, 1, 10, minutes_later, tzinfo=timezone.utc)
        drv, backend = make(tmp_path, utcnow=lambda: now)
        task = pending_task(drv.store, submitted=False, phase="queued", created_at=created)
        return await drv.task_status(task["task_id"], 0), backend
    fresh, b1 = run(main(1))
    assert fresh.status == "pending" and b1.calls == []
    stale, b2 = run(main(50))                               # 900 с очереди + 300 с запаса = 20 минут
    assert stale.status == "failed" and "прервана до отправки" in stale.error and b2.calls == []


def test_resume_does_not_fail_the_task_when_the_lock_stays_busy(tmp_path):
    async def main():
        drv, backend = make(tmp_path, queue_timeout_s=2)
        holder = FileLock(tmp_path / "web.lock")
        assert holder.try_acquire()
        task = pending_task(drv.store, post_id=POST)
        try:
            return await drv.task_status(task["task_id"], 5), backend
        finally:
            holder.release()
    info, backend = run(main())
    assert info.status == "pending" and backend.calls == []


def test_new_task_fails_with_a_clear_message_when_the_queue_never_frees(tmp_path):
    async def main():
        drv, backend = make(tmp_path, queue_timeout_s=2)
        holder = FileLock(tmp_path / "web.lock")
        assert holder.try_acquire()
        try:
            info = await drv.submit_video(VideoRequest(prompt="x"))
            return await drv.task_status(info.task_id, 10), backend
        finally:
            holder.release()
    done, backend = run(main())
    assert done.status == "failed" and "очередь" in done.error and backend.calls == []


@pytest.mark.parametrize("bad", ["ffffffffffff", "../../etc/passwd", ""])
def test_unknown_or_malformed_task_id_is_a_driver_error(tmp_path, bad):
    drv, _ = make(tmp_path)
    with pytest.raises(DriverError, match="не найдена"):
        run(drv.task_status(bad, 0))


def test_list_tasks_newest_first(tmp_path):
    drv, _ = make(tmp_path)
    old = pending_task(drv.store, created_at="2026-10-01T09:00:00+00:00", post_id=POST)
    new = pending_task(drv.store, created_at="2026-10-01T10:00:00+00:00", post_id=POST)
    assert [t.task_id for t in run(drv.list_tasks(10))] == [new["task_id"], old["task_id"]]


@pytest.mark.parametrize("phase, word", [("queued", "очереди"), ("submitting", "отправляется"),
                                         ("generating", "генерирует"), ("downloading", "скачивается")])
def test_phase_is_visible_in_the_message(phase, word):
    from grok_imagine_mcp.drivers import to_info
    msg = to_info({"task_id": "a" * 12, "kind": "video", "driver": "web", "status": "pending", "phase": phase,
                   "submitted": True, "created_at": now_iso()}).message
    assert word in msg and "grok_task_status" in msg


def test_status_reports_login_chrome_and_ready_states(tmp_path):
    async def one(state):
        drv, _ = make(tmp_path, FakeBackend(state=state))
        return await drv.status()
    logged_out = run(one(BackendState(chrome_up=True, logged_in=False, profile="PROF")))
    assert logged_out.ready is False and "войдите" in logged_out.detail and "PROF" in logged_out.detail
    no_window = run(one(BackendState(chrome_up=False, logged_in=None, profile="PROF")))
    assert no_window.ready is True and "не запущено" in no_window.detail
    fine = run(one(BackendState(chrome_up=True, logged_in=True, profile="PROF")))
    assert fine.ready is True and fine.live is True and fine.driver == "web"
    assert "условиям xAI" in fine.detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_web_driver.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'grok_imagine_mcp.web_driver'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/web_driver.py`:

```python
"""Драйвер web: Grok Imagine через окно Chrome (подписка, без API-ключа)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .drivers import DriverError, load_task, now_iso, to_info
from .models import DriverStatus, ImageRequest, TaskInfo, VideoRequest
from .store import TaskStore
from .web_lock import FileLock
from .web_options import image_options, video_options
from .web_types import Generated, WebBackend

log = logging.getLogger("grok_imagine_mcp")

POLL_INTERVAL_S = 1.0
ORPHAN_GRACE_S = 300.0          # к таймауту очереди: столько задача может идти от блокировки до отправки


class _TaskReporter:
    """Пишет фазу и post_id в запись задачи, чтобы их видели все процессы сервера."""

    def __init__(self, store: TaskStore, task: dict):
        self._store, self._task = store, task

    def phase(self, name: str) -> None:
        self._task["phase"] = name
        self._store.save(self._task)

    def submitted(self, post_id: str) -> None:
        self._task.update(post_id=post_id, submitted=True, phase="generating")
        self._store.save(self._task)


class WebDriver:
    name = "web"

    def __init__(self, backend: WebBackend, store: TaskStore, output_dir: Path, lock_path: Path, *,
                 queue_timeout_s: float = 900.0, generation_timeout_s: float = 900.0,
                 sleep=asyncio.sleep, now: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.backend, self.store, self.output_dir, self.lock_path = backend, store, Path(output_dir), Path(lock_path)
        self.queue_timeout_s, self.generation_timeout_s = queue_timeout_s, generation_timeout_s
        self._sleep, self._now, self._utcnow = sleep, now, utcnow
        self._workers: dict[str, asyncio.Task] = {}

    # ---------- постановка ----------

    async def submit_video(self, req: VideoRequest) -> TaskInfo:
        opts, notes = video_options(req)                       # отказ до создания задачи
        return self._enqueue("video", req, opts, notes)

    async def submit_image(self, req: ImageRequest) -> TaskInfo:
        opts, notes = image_options(req)
        return self._enqueue("image", req, opts, notes)

    def _enqueue(self, kind: str, req, opts, notes: list[str]) -> TaskInfo:
        task = {"task_id": self.store.new_id(), "kind": kind, "driver": self.name, "status": "pending",
                "phase": "queued", "submitted": False, "created_at": now_iso(),
                "request": req.model_dump(mode="json", exclude_none=True), "notes": notes}
        self.store.save(task)
        self._spawn(task["task_id"], opts)
        return to_info(task)

    def _spawn(self, task_id: str, opts) -> None:
        worker = asyncio.get_running_loop().create_task(self._work(task_id, opts))
        self._workers[task_id] = worker

        def forget(done: asyncio.Task) -> None:
            if self._workers.get(task_id) is done:
                del self._workers[task_id]

        worker.add_done_callback(forget)

    # ---------- исполнитель ----------

    async def _work(self, task_id: str, opts) -> None:
        resume = opts is None
        lock = FileLock(self.lock_path)                        # своя блокировка на каждого исполнителя
        try:
            if not await lock.acquire(self.queue_timeout_s, sleep=self._sleep, now=self._now):
                if not resume:                                 # у возобновления исходный исполнитель может быть жив
                    self._fail(load_task(self.store, task_id),
                               f"очередь к окну Chrome не освободилась за {int(self.queue_timeout_s)} с; "
                               "запустите задачу заново")
                return
            try:
                task = load_task(self.store, task_id)
                if task["status"] != "pending":                # пока ждали очередь, задачу уже довели
                    return
                try:
                    generated = await asyncio.wait_for(
                        self._call_backend(task, opts, _TaskReporter(self.store, task)), self.generation_timeout_s)
                    self._finish(task, generated)
                except asyncio.TimeoutError:
                    self._fail(task, f"Grok не завершил генерацию за {int(self.generation_timeout_s)} с")
                except DriverError as e:
                    self._fail(task, str(e))
                except Exception as e:
                    self._fail(task, f"{type(e).__name__}: {e}")
            finally:
                lock.release()
        except Exception:                                      # сбой самого хранилища: исполнитель не должен молчать
            log.exception("web-драйвер: исполнитель задачи %s упал", task_id)

    async def _call_backend(self, task: dict, opts, reporter) -> Generated:
        if task.get("post_id"):
            return await self.backend.resume(task["post_id"], task["kind"], reporter)
        reporter.phase("submitting")
        return await self.backend.generate(opts, reporter)

    def _finish(self, task: dict, generated: Generated) -> None:
        dest = self.output_dir / f"{task['task_id']}{generated.ext}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            part.write_bytes(generated.data)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)                       # оборванная запись не оставляет полфайла
        task.update(status="done", output_path=str(dest), error=None)
        task.pop("phase", None)
        self.store.save(task)

    def _fail(self, task: dict, message: str) -> None:
        task.update(status="failed", error=message)
        task.pop("phase", None)
        self.store.save(task)

    # ---------- статус ----------

    async def task_status(self, task_id: str, wait_s: int) -> TaskInfo:
        task = load_task(self.store, task_id)
        if task["status"] == "pending" and task_id not in self._workers:
            task = self._adopt(task)
        deadline = self._now() + wait_s
        while task["status"] == "pending" and self._now() < deadline:
            await self._sleep(POLL_INTERVAL_S)
            task = load_task(self.store, task_id)
        return to_info(task)

    def _adopt(self, task: dict) -> dict:
        """Задача «висит», а исполнителя в этом процессе нет: сервер перезапускали или она чужая."""
        if task.get("post_id"):
            self._spawn(task["task_id"], None)                 # дожимаем по post_id
            return task
        age = (self._utcnow() - datetime.fromisoformat(task["created_at"])).total_seconds()
        if age > self.queue_timeout_s + ORPHAN_GRACE_S:
            self._fail(task, "задача прервана до отправки в Grok (сервер был перезапущен); запустите её заново")
        return task

    async def list_tasks(self, limit: int) -> list[TaskInfo]:
        return [to_info(t) for t in self.store.list(limit)]

    async def status(self) -> DriverStatus:
        st = await self.backend.state()
        if st.logged_in is False:
            detail = f"Grok просит войти в аккаунт: откройте окно Chrome (профиль {st.profile}) и войдите вручную."
        elif not st.chrome_up:
            detail = ("Окно Chrome для Grok не запущено: оно откроется при первой генерации. "
                      "Первый вход в аккаунт выполните вручную в этом окне.")
        else:
            detail = "Генерации идут через окно Chrome по подписке Grok."
        detail += (f" Результаты: {self.output_dir}. "
                   "Автоматизация веб-интерфейса подписки может не соответствовать условиям xAI.")
        return DriverStatus(driver=self.name, ready=st.logged_in is not False, live=True, detail=detail)
```

В `src/grok_imagine_mcp/drivers.py` заменить функцию `describe` (строки 37–53) на:

```python
_WEB_PHASES = {
    "queued": "Задача в очереди к окну Chrome с Grok.",
    "submitting": "Задача отправляется в Grok.",
    "generating": "Grok генерирует результат.",
    "downloading": "Результат готов, файл скачивается.",
}


def _describe(task: dict) -> str:
    st, kind = task["status"], "Видео" if task["kind"] == "video" else "Картинка"
    if st == "recorded_dry_run":
        return "Драйвер xai не подключён (dry-run): задача записана на диск, в xAI НЕ отправлена."
    if st == "pending" and task.get("driver") == "web":
        return (_WEB_PHASES.get(task.get("phase"), "Задача выполняется в Grok.")
                + " Проверьте grok_task_status позже (можно с wait_s=45).")
    if st == "pending":
        return "Задача принята xAI и выполняется. Проверьте grok_task_status позже (можно с wait_s=45)."
    if st == "done" and task.get("output_path"):
        return f"{kind} готово: {task['output_path']}"
    if st == "done":
        return (f"{kind} готово, но скачать не удалось: {task.get('error')}. "
                "Повторите grok_task_status: ссылка на результат действует 24 часа.")
    if st == "failed":
        return f"Генерация не удалась: {task.get('error')}"
    if st == "expired":
        return "Запрос истёк на стороне xAI. Запустите генерацию заново."
    return st


def describe(task: dict) -> str:
    """Человекочитаемое состояние задачи для ответа модели."""
    text = _describe(task)
    notes = task.get("notes")
    return f"{text} Примечания: {'; '.join(notes)}." if notes else text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_web_driver.py tests\test_drivers.py tests\test_xai_driver.py tests\test_server.py -q`
Expected: все тесты проходят (новые `test_web_driver.py` и прежние, то есть `describe()` для `xai` и `dry-run` не изменился).

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/web_driver.py src/grok_imagine_mcp/drivers.py tests/test_web_driver.py
git commit -m "feat(grok-imagine-mcp): драйвер web: очередь, блокировка, возобновление по post_id" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Страница Grok (`grok_page.py`)

**Files:**
- Create: `src/grok_imagine_mcp/grok_page.py`
- Create: `tests/test_grok_page.py`

**Interfaces:**
- Consumes: `CdpConnection` (методы `send`, `wait_event`, `evaluate`; Task 1), `check_mp4`, `detect_image_ext`, `MediaError` (Task 2), `VideoOptions`, `ImageOptions` (Task 5), `Generated` (Task 5), `DriverError`.
- Produces:
  - Константы: `IMAGINE_URL`, `POST_URL` (с `{post_id}`), `POST_ID_RE`, `GROK_HOSTS`, подписи `L_MODE`, `L_RESOLUTION`, `L_DURATION`, `L_ASPECT`, `L_AUDIO`, `L_UPLOAD`.
  - `def check_asset_url(url: str) -> None` — `DriverError`, если адрес не `https://…grok.com`.
  - `class GrokPage(cdp, *, profile: str = "", sleep=asyncio.sleep, now=time.monotonic, poll_s: float = 1.0)` с методами:
    - `async open(url: str, *, post: bool = False) -> None`
    - `async prepare_video(opts: VideoOptions) -> None`, `async prepare_image(opts: ImageOptions) -> None`
    - `async choose_menu_option(button_labels: tuple[str, ...], option: str) -> None`
    - `async enter_prompt(text: str) -> None`
    - `async submit() -> str` — возвращает `post_id`
    - `async wait_result(kind: str, post_id: str) -> str` — URL результата; без собственного общего таймаута (его держит драйвер), но с выходом по «пусто»: нет ни прогресса, ни результата `IDLE_LIMIT_S = 45` секунд подряд.
    - `async download(url: str, kind: str) -> Generated`
  - Все JS-фрагменты начинаются с комментария-метки `/* NAME */`, по ним тесты подставляют ответы.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_grok_page.py`:

```python
import asyncio
import base64
import json
import struct

import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.grok_page import L_DURATION, GrokPage, check_asset_url
from grok_imagine_mcp.web_options import VideoOptions


def _box(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


GOOD_MP4 = _box(b"ftyp", b"isom0000") + _box(b"moov", b"m" * 20) + _box(b"mdat", b"d" * 100)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
URL = "https://assets.grok.com/users/u/generated/11111111-2222-3333-4444-555555555555/generated_video.mp4?x=1"
POST = "11111111-2222-3333-4444-555555555555"


class FakeCdp:
    """Подставляет ответы по метке JS-фрагмента. Список в ответе отдаётся по одному, последний повторяется."""

    def __init__(self, script=None):
        self.script = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (script or {}).items()}
        self.sent, self.events, self.evaluated = [], {}, []

    async def evaluate(self, js):
        self.evaluated.append(js)
        for key, queue in self.script.items():
            if key in js:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"неожиданный JS: {js[:70]}")

    async def send(self, method, **params):
        self.sent.append((method, params))
        return {}

    async def wait_event(self, name, timeout=5.0):
        return self.events.get(name)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def page(script=None, **kw):
    cdp, clock = FakeCdp(script), Clock()
    return GrokPage(cdp, profile="PROF", sleep=clock.sleep, now=clock.now, poll_s=1.0, **kw), cdp, clock


def state(**over):
    base = {"href": "https://grok.com/imagine", "path": "/imagine", "textbox": True, "ready": True,
            "progress": False, "dialog": None, "vids": [], "imgs": [], "text": "Что мы должны imagine?"}
    return json.dumps({**base, **over})


def run(coro):
    return asyncio.run(coro)


# ---------- вход и загрузка ----------

def test_open_asks_to_log_in_when_redirected_to_the_login_host():
    p, _, _ = page({"/* STATE */": state(href="https://accounts.x.ai/sign-in", textbox=False)})
    with pytest.raises(DriverError) as e:
        run(p.open("https://grok.com/imagine"))
    assert "войти в аккаунт" in str(e.value) and "PROF" in str(e.value) and "не вводит" in str(e.value)


def test_open_returns_once_the_composer_is_there():
    p, cdp, _ = page({"/* STATE */": [state(textbox=False), state()]})
    run(p.open("https://grok.com/imagine"))
    assert ("Page.navigate", {"url": "https://grok.com/imagine"}) in cdp.sent


def test_open_times_out_with_the_address_in_the_message():
    p, _, _ = page({"/* STATE */": state(textbox=False)})
    with pytest.raises(DriverError, match="не загрузилась"):
        run(p.open("https://grok.com/imagine"))


def test_open_reports_a_blocking_dialog_but_ignores_cookie_banner():
    p, _, _ = page({"/* STATE */": state(dialog="Достигнут дневной лимит генераций")})
    with pytest.raises(DriverError, match="лимит"):
        run(p.open("https://grok.com/imagine"))


# ---------- элементы интерфейса ----------

def test_missing_mode_switch_lists_the_labels_that_are_on_the_page():
    p, _, _ = page({"/* CLICK_RADIO */": False, "/* LABELS */": json.dumps(["Звук видео", "Загрузить"])})
    with pytest.raises(DriverError) as e:
        run(p.select_mode("video"))
    assert "интерфейс Grok изменился" in str(e.value) and "Звук видео" in str(e.value)


def _item(text, disabled=False):
    return {"text": text, "first": text.split()[0], "x": 10.0, "y": 20.0, "disabled": disabled}


def test_menu_value_that_does_not_exist_lists_the_available_ones():
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}),
                      "/* MENU_ITEMS */": json.dumps([_item("6s"), _item("10s"), _item("15s")])})
    with pytest.raises(DriverError) as e:
        run(p.choose_menu_option(L_DURATION, "8s"))
    assert "нет значения 8s" in str(e.value) and "6s, 10s, 15s" in str(e.value)
    assert any(m == "Input.dispatchKeyEvent" for m, _ in cdp.sent)            # меню закрыто Escape


def test_disabled_menu_value_is_reported_as_unavailable():
    p, _, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}),
                    "/* MENU_ITEMS */": json.dumps([_item("480p"), _item("1080p", disabled=True)])})
    with pytest.raises(DriverError, match="недоступно"):
        run(p.choose_menu_option(("Разрешение видео",), "1080p"))


def test_menu_value_is_chosen_with_real_mouse_events():
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 5, "y": 6}),
                      "/* MENU_ITEMS */": json.dumps([_item("6s"), _item("10s")]), "/* MENU_OPEN */": False})
    run(p.choose_menu_option(L_DURATION, "10s"))
    presses = [params for m, params in cdp.sent if m == "Input.dispatchMouseEvent" and params["type"] == "mousePressed"]
    assert [(q["x"], q["y"]) for q in presses] == [(5, 6), (10.0, 20.0)]


def test_prompt_newlines_are_collapsed_so_the_prompt_is_not_sent_early():
    p, cdp, _ = page({"/* FOCUS */": True, "/* SUBMIT_READY */": True})
    run(p.enter_prompt("первая строка\n\nвторая   строка\r\nтретья"))
    assert ("Input.insertText", {"text": "первая строка вторая строка третья"}) in cdp.sent


def test_submit_returns_post_id_from_the_address():
    p, _, _ = page({"/* SUBMIT_CLICK */": True,
                    "/* STATE */": [state(path="/imagine"), state(path=f"/imagine/post/{POST}")]})
    assert run(p.submit()) == POST


def test_submit_without_the_page_moving_on_is_an_error():
    p, _, _ = page({"/* SUBMIT_CLICK */": True, "/* STATE */": state(path="/imagine")})
    with pytest.raises(DriverError, match="не принял запрос"):
        run(p.submit())


def test_audio_toggle_is_clicked_only_when_the_state_differs():
    p, cdp, _ = page({"/* AUDIO_STATE */": "true", "/* RECT */": json.dumps({"x": 1, "y": 2})})
    run(p.set_audio(True))
    assert not [1 for m, _ in cdp.sent if m == "Input.dispatchMouseEvent"]
    p, cdp, _ = page({"/* AUDIO_STATE */": "true", "/* RECT */": json.dumps({"x": 1, "y": 2})})
    run(p.set_audio(False))
    assert [1 for m, _ in cdp.sent if m == "Input.dispatchMouseEvent"]


def test_attach_image_uses_the_intercepted_file_chooser(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}), "/* PANEL_UPLOAD */": json.dumps({"x": 3, "y": 4}),
                      "/* THUMB */": True})
    cdp.events["Page.fileChooserOpened"] = {"backendNodeId": 42, "mode": "selectMultiple"}
    run(p.attach_image(img))
    assert ("DOM.setFileInputFiles", {"files": [str(img)], "backendNodeId": 42}) in cdp.sent
    assert ("Page.setInterceptFileChooserDialog", {"enabled": True}) in cdp.sent
    assert cdp.sent[-1] == ("Page.setInterceptFileChooserDialog", {"enabled": False})


def test_attach_image_fails_clearly_when_no_chooser_opens(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}), "/* PANEL_UPLOAD */": json.dumps({"x": 3, "y": 4})})
    with pytest.raises(DriverError, match="выбор файла"):
        run(p.attach_image(img))
    assert cdp.sent[-1] == ("Page.setInterceptFileChooserDialog", {"enabled": False})


# ---------- ожидание результата ----------

def test_wait_result_returns_the_url_after_progress_is_gone():
    p, _, _ = page({"/* STATE */": [state(progress=True), state(progress=True, vids=[URL]), state(vids=[URL])]})
    assert run(p.wait_result("video", POST)) == URL


def test_wait_result_for_images_reads_the_image_list():
    img = "https://assets.grok.com/users/u/generated/x/image.jpg"
    p, _, _ = page({"/* STATE */": state(imgs=[img])})
    assert run(p.wait_result("image", POST)) == img


def test_wait_result_raises_on_a_blocking_dialog():
    p, _, _ = page({"/* STATE */": state(progress=True, dialog="Запрос отклонён модерацией")})
    with pytest.raises(DriverError, match="модерацией"):
        run(p.wait_result("video", POST))


def test_wait_result_gives_up_when_nothing_happens_and_shows_the_page_text():
    p, _, _ = page({"/* STATE */": state(text="Что-то пошло не так. Повторите попытку")})
    with pytest.raises(DriverError) as e:
        run(p.wait_result("video", POST))
    assert "ни прогресса, ни результата" in str(e.value) and "Повторите попытку" in str(e.value)


# ---------- скачивание ----------

def _fetched(data, **over):
    return json.dumps({"ok": True, "status": 206, "size": len(data), "b64": base64.b64encode(data).decode(), **over})


def test_download_returns_a_verified_video():
    p, _, _ = page({"/* FETCH */": _fetched(GOOD_MP4)})
    got = run(p.download(URL, "video"))
    assert got.data == GOOD_MP4 and got.ext == ".mp4"


def test_download_rejects_a_truncated_video():
    p, _, _ = page({"/* FETCH */": _fetched(GOOD_MP4[:-5])})
    with pytest.raises(DriverError, match="повреждён"):
        run(p.download(URL, "video"))


def test_download_rejects_an_html_error_page_saved_as_video():
    p, _, _ = page({"/* FETCH */": _fetched(b"<html>403</html>")})
    with pytest.raises(DriverError, match="повреждён"):
        run(p.download(URL, "video"))


def test_download_reports_network_failure_and_http_errors():
    p, _, _ = page({"/* FETCH */": json.dumps({"err": "TypeError: Failed to fetch"})})
    with pytest.raises(DriverError, match="не удалось скачать"):
        run(p.download(URL, "video"))
    p, _, _ = page({"/* FETCH */": json.dumps({"ok": False, "status": 403, "size": 0, "b64": ""})})
    with pytest.raises(DriverError, match="403"):
        run(p.download(URL, "video"))


def test_download_detects_the_image_type():
    p, _, _ = page({"/* FETCH */": _fetched(PNG)})
    got = run(p.download("https://assets.grok.com/users/u/generated/x/image.png", "image"))
    assert got.ext == ".png"


def test_asset_url_must_be_https_on_grok_com_and_nothing_is_fetched_otherwise():
    check_asset_url("https://assets.grok.com/a.mp4")
    check_asset_url("https://grok.com/a.mp4")
    for bad in ("http://assets.grok.com/a.mp4", "https://evil.example/a.mp4", "https://grok.com.evil.example/a",
                "file:///C:/x.mp4"):
        with pytest.raises(DriverError, match="неожиданного адреса"):
            check_asset_url(bad)
    p, cdp, _ = page()                                           # пустой сценарий: любой evaluate упал бы
    with pytest.raises(DriverError, match="неожиданного адреса"):
        run(p.download("https://evil.example/a.mp4", "video"))
    assert cdp.evaluated == []


# ---------- подготовка целиком ----------

def test_prepare_video_applies_only_the_options_that_were_given():
    p, cdp, _ = page({"/* CLICK_RADIO */": True, "/* RADIO_CHECKED */": True})
    run(p.prepare_video(VideoOptions("x", None, None, None, None, None)))
    assert not [1 for m, _ in cdp.sent if m == "Input.dispatchMouseEvent"]       # ни одного меню не открывали
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_grok_page.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'grok_imagine_mcp.grok_page'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/grok_page.py`:

```python
"""Страница Grok Imagine: всё, что зависит от вёрстки grok.com. При смене сайта ломается только этот файл.

Подписи элементов русские (подтверждены живой пробой 2026-09-30); английские варианты в таблицах — запасные
кандидаты, пробой не проверены. Меню параметров (Radix) открываются только настоящими событиями мыши,
обычные кнопки принимают click().
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cdp import CdpConnection, CdpError
from .drivers import DriverError
from .media_check import MediaError, check_mp4, detect_image_ext
from .web_options import ImageOptions, VideoOptions
from .web_types import Generated

IMAGINE_URL = "https://grok.com/imagine"
POST_URL = "https://grok.com/imagine/post/{post_id}"
POST_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
GROK_HOSTS = frozenset({"grok.com", "www.grok.com"})
IDLE_LIMIT_S = 45.0
LOAD_TIMEOUT_S = 45.0

L_MODE = {"video": ("Видео", "Video"), "image": ("Изображение", "Image")}
L_RESOLUTION = ("Разрешение видео", "Video resolution")
L_DURATION = ("Длительность видео", "Video duration")
L_ASPECT = ("Соотношение сторон", "Aspect ratio")
L_AUDIO = ("Звук видео", "Video sound", "Video audio")
L_UPLOAD = ("Загрузить", "Upload")
PROGRESS_WORDS = ("Создаётся", "Generating", "Creating")

# ---------- JS-фрагменты (метка в начале нужна тестам; __X__ подставляется перед отправкой) ----------

_STATE = r"""/* STATE */ (() => {
  const root = document.getElementById('grok-content-area') || document.body;
  const dialogs = [...document.querySelectorAll('[role=dialog],[role=alertdialog]')]
    .map(d => (d.innerText || '').trim().replace(/\s+/g, ' '))
    .filter(t => t && !/cookie|конфиденциальн|privacy/i.test(t));
  const post = __POST_ID__;
  const vids = [...document.querySelectorAll('video')].map(v => v.currentSrc || v.src).filter(s => s && (!post || s.includes(post)));
  const imgs = [...document.querySelectorAll('img')].filter(i => i.naturalWidth >= 256).map(i => i.currentSrc || i.src).filter(s => s && (!post || s.includes(post)));
  const text = (root.innerText || '').replace(/\s+/g, ' ').trim();
  return JSON.stringify({href: location.href, path: location.pathname, textbox: !!document.querySelector('[role=textbox]'),
    ready: document.readyState === 'complete', progress: __PROGRESS_RE__.test(text), dialog: dialogs[0] || null,
    vids, imgs, text: text.slice(0, 200)});
})()"""

_RECT = r"""/* RECT */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('button,[role=radio]')].find(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && L.includes((e.getAttribute('aria-label') || '').trim());
  });
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
})()"""

_CLICK_RADIO = r"""/* CLICK_RADIO */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('[role=radio]')].find(e => e.getBoundingClientRect().width > 0 && L.includes((e.getAttribute('aria-label') || '').trim()));
  if (!b) return false;
  b.click();
  return true;
})()"""

_RADIO_CHECKED = r"""/* RADIO_CHECKED */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('[role=radio]')].find(e => e.getBoundingClientRect().width > 0 && L.includes((e.getAttribute('aria-label') || '').trim()));
  return !!b && b.getAttribute('aria-checked') === 'true';
})()"""

_MENU_ITEMS = r"""/* MENU_ITEMS */ (() => {
  const menu = [...document.querySelectorAll('[role=menu]')].find(m => m.getBoundingClientRect().width > 0 && m.querySelector('[role=menuitemradio]'));
  if (!menu) return null;
  return JSON.stringify([...menu.querySelectorAll('[role=menuitemradio]')].map(e => {
    const r = e.getBoundingClientRect();
    const text = (e.innerText || '').trim().replace(/\s+/g, ' ');
    return {text, first: text.split(' ')[0], x: r.x + r.width / 2, y: r.y + r.height / 2,
            disabled: e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('data-disabled')};
  }));
})()"""

_MENU_OPEN = r"""/* MENU_OPEN */ [...document.querySelectorAll('[role=menu]')].some(m => m.getBoundingClientRect().width > 0)"""

_AUDIO_STATE = r"""/* AUDIO_STATE */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('button')].find(e => e.getBoundingClientRect().width > 0 && L.includes((e.getAttribute('aria-label') || '').trim()));
  if (!b) return null;
  const v = b.getAttribute('aria-checked') ?? b.getAttribute('aria-pressed');
  return v === null ? 'unknown' : v;
})()"""

_PANEL_UPLOAD = r"""/* PANEL_UPLOAD */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('button')].find(e => !e.getAttribute('aria-label') && L.includes((e.innerText || '').trim()) && e.getBoundingClientRect().width > 0);
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
})()"""

_THUMB = r"""/* THUMB */ !!document.querySelector('img[src^="blob:"]')"""
_FOCUS = r"""/* FOCUS */ (() => { const t = document.querySelector('[role=textbox]'); if (!t) return false; t.focus(); return true; })()"""
_SUBMIT_READY = r"""/* SUBMIT_READY */ (() => { const b = document.querySelector('button[type=submit]'); return !!b && !b.disabled && b.getBoundingClientRect().width > 0; })()"""
_SUBMIT_CLICK = r"""/* SUBMIT_CLICK */ (() => { const b = document.querySelector('button[type=submit]'); if (!b || b.disabled) return false; b.click(); return true; })()"""
_LABELS = r"""/* LABELS */ JSON.stringify([...document.querySelectorAll('button,[role=radio]')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.x >= 260; }).map(e => (e.getAttribute('aria-label') || '').trim()).filter(Boolean))"""

_FETCH = r"""/* FETCH */ (async (u) => {
  try {
    const r = await fetch(u, {credentials: 'include'});
    const b = new Uint8Array(await r.arrayBuffer());
    let s = '';
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return JSON.stringify({ok: r.ok, status: r.status, size: b.length, b64: r.ok ? btoa(s) : ''});
  } catch (e) { return JSON.stringify({err: String(e)}); }
})(__URL__)"""

_PROGRESS_RE = "/(" + "|".join(PROGRESS_WORDS) + ")/"


def _labels(js: str, labels) -> str:
    return js.replace("__LABELS__", json.dumps(list(labels)))


def check_asset_url(url: str) -> None:
    """fetch идёт из страницы с куками пользователя, поэтому берём только адреса самого Grok."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "grok.com" or host.endswith(".grok.com")):
        raise DriverError(f"Grok отдал результат с неожиданного адреса ({host or url[:40]}); скачивание отменено")


class GrokPage:
    def __init__(self, cdp: CdpConnection, *, profile: str = "", sleep=asyncio.sleep, now=time.monotonic,
                 poll_s: float = 1.0):
        self.cdp, self.profile, self._sleep, self._now, self.poll_s = cdp, profile, sleep, now, poll_s

    # ---------- низкий уровень ----------

    def _need_login(self) -> DriverError:
        return DriverError(f"Grok просит войти в аккаунт. Откройте окно Chrome (профиль {self.profile}), войдите "
                           "вручную и повторите. Драйвер логин, пароли и коды не вводит.")

    async def _state(self, post_id: str = "") -> dict:
        js = _STATE.replace("__POST_ID__", json.dumps(post_id)).replace("__PROGRESS_RE__", _PROGRESS_RE)
        return json.loads(await self.cdp.evaluate(js))

    async def _missing(self, what: str) -> DriverError:
        try:
            found = json.loads(await self.cdp.evaluate(_LABELS))
        except (CdpError, ValueError, TypeError):
            found = []
        shown = ", ".join(found[:30]) or "ничего не найдено"
        return DriverError(f"интерфейс Grok изменился или не успел загрузиться: не найден элемент «{what}». "
                           f"Подписи на странице: {shown}")

    async def _wait_for(self, js: str, timeout_s: float, what: str):
        deadline = self._now() + timeout_s
        while True:
            value = await self.cdp.evaluate(js)
            if value:
                return value
            if self._now() >= deadline:
                raise await self._missing(what)
            await self._sleep(0.4)

    async def _rect(self, labels) -> tuple[float, float] | None:
        raw = await self.cdp.evaluate(_labels(_RECT, labels))
        if not raw:
            return None
        point = json.loads(raw)
        return point["x"], point["y"]

    async def _real_click(self, x: float, y: float) -> None:
        await self.cdp.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        await self.cdp.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        await self.cdp.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)

    async def _escape(self) -> None:
        for kind in ("keyDown", "keyUp"):
            await self.cdp.send("Input.dispatchKeyEvent", type=kind, key="Escape", code="Escape",
                                windowsVirtualKeyCode=27)

    # ---------- загрузка страницы ----------

    async def open(self, url: str, *, post: bool = False) -> None:
        """Открывает страницу и ждёт композер (post=True: страницу результата, ей достаточно загрузиться)."""
        await self.cdp.send("Page.enable")
        await self.cdp.send("Page.navigate", url=url)
        await self.cdp.wait_event("Page.loadEventFired", 30.0)       # иначе можно прочитать ещё старый документ
        deadline = self._now() + LOAD_TIMEOUT_S
        while True:
            st = await self._state()
            parts = urlsplit(st["href"])
            if (parts.hostname or "") not in GROK_HOSTS:
                raise self._need_login()
            if st["dialog"]:
                raise DriverError(f"Grok показал окно, которое мешает работе: {st['dialog'][:200]}")
            if st["textbox"] or (post and st["ready"]):
                return
            if self._now() >= deadline:
                raise DriverError(f"страница Grok не загрузилась за {int(LOAD_TIMEOUT_S)} с "
                                  f"(адрес: {parts.hostname}{parts.path})")
            await self._sleep(self.poll_s)

    # ---------- режим и параметры ----------

    async def select_mode(self, kind: str) -> None:
        labels = L_MODE[kind]
        if not await self.cdp.evaluate(_labels(_CLICK_RADIO, labels)):
            raise await self._missing(f"переключатель режима «{labels[0]}»")
        await self._wait_for(_labels(_RADIO_CHECKED, labels), 6.0, f"режим «{labels[0]}» не включился")

    async def choose_menu_option(self, button_labels, option: str) -> None:
        pos = await self._rect(button_labels)
        if pos is None:
            raise await self._missing(f"кнопка «{button_labels[0]}»")
        await self._real_click(*pos)
        raw = await self._wait_for(_MENU_ITEMS, 4.0, f"меню «{button_labels[0]}»")
        items = json.loads(raw)
        hit = next((i for i in items if i["first"] == option), None)
        if hit is None:
            await self._escape()
            raise DriverError(f"в меню «{button_labels[0]}» нет значения {option}. "
                              f"Есть: {', '.join(i['first'] for i in items)}")
        if hit["disabled"]:
            await self._escape()
            raise DriverError(f"значение {option} в меню «{button_labels[0]}» недоступно "
                              "(возможно, не входит в вашу подписку)")
        await self._real_click(hit["x"], hit["y"])
        await self._sleep(0.6)
        if await self.cdp.evaluate(_MENU_OPEN):
            await self._escape()

    async def set_audio(self, wanted: bool) -> None:
        state = await self.cdp.evaluate(_labels(_AUDIO_STATE, L_AUDIO))
        if state is None:
            raise await self._missing(f"переключатель «{L_AUDIO[0]}»")
        if state not in ("true", "false"):
            raise DriverError(f"не удалось определить состояние переключателя «{L_AUDIO[0]}»")
        if (state == "true") != wanted:
            pos = await self._rect(L_AUDIO)
            if pos is None:
                raise await self._missing(f"переключатель «{L_AUDIO[0]}»")
            await self._real_click(*pos)
            await self._sleep(0.4)

    async def attach_image(self, path: Path) -> None:
        await self.cdp.send("Page.enable")
        await self.cdp.send("Page.setInterceptFileChooserDialog", enabled=True)   # системное окно не должно появиться
        try:
            pos = await self._rect(L_UPLOAD)
            if pos is None:
                raise await self._missing(f"кнопка «{L_UPLOAD[0]}»")
            await self._real_click(*pos)
            await self._sleep(1.2)                                   # панель «Генерации / Загрузки» открывается
            raw = await self._wait_for(_labels(_PANEL_UPLOAD, L_UPLOAD), 6.0, "кнопка загрузки в панели")
            point = json.loads(raw)
            await self._real_click(point["x"], point["y"])
            event = await self.cdp.wait_event("Page.fileChooserOpened", 6.0)
            if event is None:
                raise DriverError("Grok не открыл выбор файла: не удалось приложить фото")
            await self.cdp.send("DOM.setFileInputFiles", files=[str(path)], backendNodeId=event["backendNodeId"])
            await self._wait_for(_THUMB, 25.0, "миниатюра приложенного фото")
        finally:
            try:
                await self.cdp.send("Page.setInterceptFileChooserDialog", enabled=False)
            except CdpError:
                pass

    async def prepare_video(self, opts: VideoOptions) -> None:
        await self.select_mode("video")
        if opts.image_path is not None:
            await self.attach_image(opts.image_path)
        if opts.resolution:
            await self.choose_menu_option(L_RESOLUTION, opts.resolution)
        if opts.duration_s:
            await self.choose_menu_option(L_DURATION, f"{opts.duration_s}s")
        if opts.aspect_ratio:                                        # с фото его уже нет: драйвер передаёт None
            await self.choose_menu_option(L_ASPECT, opts.aspect_ratio)
        if opts.generate_audio is not None:
            await self.set_audio(opts.generate_audio)

    async def prepare_image(self, opts: ImageOptions) -> None:
        await self.select_mode("image")
        if opts.aspect_ratio:
            await self.choose_menu_option(L_ASPECT, opts.aspect_ratio)

    # ---------- промт, отправка, результат ----------

    async def enter_prompt(self, text: str) -> None:
        if not await self.cdp.evaluate(_FOCUS):
            raise await self._missing("поле ввода промта")
        await self.cdp.send("Input.insertText", text=" ".join(text.split()))   # перенос строки мог бы отправить форму
        await self._wait_for(_SUBMIT_READY, 6.0, "кнопка отправки")

    async def submit(self) -> str:
        if not await self.cdp.evaluate(_SUBMIT_CLICK):
            raise await self._missing("кнопка отправки")
        deadline = self._now() + 30.0
        while True:
            st = await self._state()
            if st["dialog"]:
                raise DriverError(f"Grok не принял запрос и показал окно: {st['dialog'][:200]}")
            match = re.search(r"/imagine/post/([0-9a-f-]{36})", st["path"])
            if match:
                return match.group(1)
            if self._now() >= deadline:
                raise DriverError("Grok не принял запрос за 30 с: страница не перешла к результату")
            await self._sleep(self.poll_s)

    async def wait_result(self, kind: str, post_id: str) -> str:
        idle_since = None
        while True:
            st = await self._state(post_id)
            if st["dialog"]:
                raise DriverError(f"Grok показал окно и не довёл генерацию до конца: {st['dialog'][:200]}")
            media = st["vids"] if kind == "video" else st["imgs"]
            if media and not st["progress"]:
                return media[0]
            if st["progress"] or media:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = self._now()
                if self._now() - idle_since > IDLE_LIMIT_S:
                    raise DriverError("Grok не показал ни прогресса, ни результата: возможно, запрос отклонён "
                                      f"(модерация) или исчерпан лимит. Текст страницы: {st['text']}")
            await self._sleep(self.poll_s * 3)

    async def download(self, url: str, kind: str) -> Generated:
        check_asset_url(url)
        res = json.loads(await self.cdp.evaluate(_FETCH.replace("__URL__", json.dumps(url))))
        if "err" in res:
            raise DriverError(f"не удалось скачать результат из Grok: {res['err']}")
        if not res.get("ok"):
            raise DriverError(f"не удалось скачать результат из Grok: HTTP {res.get('status')}")
        data = base64.b64decode(res["b64"])
        try:
            if kind == "video":
                check_mp4(data)
                return Generated(data, ".mp4")
            return Generated(data, detect_image_ext(data))
        except MediaError as e:
            raise DriverError(f"скачанный файл повреждён: {e}") from e
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_grok_page.py -q`
Expected: все тесты проходят (`25 passed`).

Если какой-то тест падает из-за расхождения метки JS (`/* NAME */`), править нужно константу в `grok_page.py` или тест так, чтобы метка в обоих местах совпадала. Логику JS тесты не проверяют (она проверяется только живым тестом в Task 10).

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/grok_page.py tests/test_grok_page.py
git commit -m "feat(grok-imagine-mcp): страница Grok Imagine: режим, параметры, фото, отправка, результат, скачивание" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Backend на CDP (`grok_backend.py`)

**Files:**
- Create: `src/grok_imagine_mcp/grok_backend.py`
- Create: `tests/test_grok_backend.py`

**Interfaces:**
- Consumes: `http_json`, `CdpConnection` (Task 1); `ensure_chrome` (Task 4); `GrokPage`, `IMAGINE_URL`, `POST_URL`, `POST_ID_RE`, `GROK_HOSTS` (Task 7); `BackendState`, `Generated`, `Reporter` (Task 5); `VideoOptions`, `ImageOptions` (Task 5).
- Produces: `class GrokBackend(port: int, profile: Path, chrome_path: Path | None, *, fetch_json=http_json, ensure=ensure_chrome, connect=CdpConnection.connect)` — реализует `WebBackend`: `async state()`, `async generate(opts, reporter)`, `async resume(post_id, kind, reporter)`. Функция `pick_target(pages: list[dict]) -> str | None` — адрес WebSocket вкладки (предпочитает вкладку на `grok.com`).

- [ ] **Step 1: Write the failing test**

Создать `tests/test_grok_backend.py`:

```python
import asyncio

import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.grok_backend import GrokBackend, pick_target
from grok_imagine_mcp.web_types import BackendState


def backend(version, pages, tmp_path):
    async def fetch_json(port, path, **kw):
        return version if path == "/json/version" else pages

    return GrokBackend(9340, tmp_path / "prof", None, fetch_json=fetch_json)


def page(url, ws="ws://x/1", kind="page"):
    return {"type": kind, "url": url, "webSocketDebuggerUrl": ws}


def test_no_window_means_chrome_is_down_and_login_unknown(tmp_path):
    st = asyncio.run(backend(None, None, tmp_path).state())
    assert st == BackendState(chrome_up=False, logged_in=None, profile=str(tmp_path / "prof"))


def test_a_tab_on_grok_means_logged_in(tmp_path):
    st = asyncio.run(backend({"Browser": "c"}, [page("https://grok.com/imagine")], tmp_path).state())
    assert st.chrome_up is True and st.logged_in is True


def test_only_a_login_tab_means_logged_out(tmp_path):
    pages = [page("https://accounts.x.ai/sign-in"), page("chrome://newtab/", kind="browser_ui")]
    assert asyncio.run(backend({"Browser": "c"}, pages, tmp_path).state()).logged_in is False


def test_unrecognised_tabs_leave_login_unknown(tmp_path):
    st = asyncio.run(backend({"Browser": "c"}, [page("about:blank")], tmp_path).state())
    assert st.chrome_up is True and st.logged_in is None


def test_grok_tab_wins_over_login_tab(tmp_path):
    pages = [page("https://accounts.google.com/x"), page("https://grok.com/imagine")]
    assert asyncio.run(backend({"Browser": "c"}, pages, tmp_path).state()).logged_in is True


def test_pick_target_prefers_grok_then_any_page_then_none():
    grok, other = page("https://grok.com/imagine", "ws://grok"), page("https://example.com", "ws://other")
    assert pick_target([other, grok]) == "ws://grok"
    assert pick_target([other, page("chrome://x", "ws://ui", kind="browser_ui")]) == "ws://other"
    assert pick_target([page("chrome://x", "ws://ui", kind="service_worker")]) is None
    assert pick_target([]) is None


def test_resume_refuses_a_malformed_post_id_before_touching_chrome(tmp_path):
    calls = []

    async def ensure(*a, **k):
        calls.append(a)

    b = GrokBackend(9340, tmp_path, None, ensure=ensure)

    class R:
        def phase(self, n): ...
        def submitted(self, p): ...

    with pytest.raises(DriverError, match="post_id"):
        asyncio.run(b.resume("../../x", "video", R()))
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_grok_backend.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'grok_imagine_mcp.grok_backend'`).

- [ ] **Step 3: Write minimal implementation**

Создать `src/grok_imagine_mcp/grok_backend.py`:

```python
"""Backend драйвера web: окно Chrome + CDP + страница Grok. Единственное место, где всё это собирается вместе."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .cdp import CdpConnection, http_json
from .chrome import ensure_chrome
from .drivers import DriverError
from .grok_page import GROK_HOSTS, IMAGINE_URL, POST_ID_RE, POST_URL, GrokPage
from .web_options import ImageOptions, VideoOptions
from .web_types import BackendState, Generated, Reporter

_LOGIN_PREFIX = "accounts."


def pick_target(pages: list[dict]) -> str | None:
    """Адрес WebSocket вкладки: сначала вкладка на grok.com, затем любая обычная вкладка."""
    tabs = [p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    for tab in tabs:
        if (urlsplit(tab.get("url", "")).hostname or "") in GROK_HOSTS:
            return tab["webSocketDebuggerUrl"]
    return tabs[0]["webSocketDebuggerUrl"] if tabs else None


class GrokBackend:
    def __init__(self, port: int, profile: Path, chrome_path: Path | None, *,
                 fetch_json=http_json, ensure=ensure_chrome, connect=CdpConnection.connect):
        self.port, self.profile, self.chrome_path = port, Path(profile), chrome_path
        self._fetch_json, self._ensure, self._connect = fetch_json, ensure, connect

    async def state(self) -> BackendState:
        """Не запускает окно и не открывает страницы: только смотрит на уже открытые вкладки."""
        if not await self._fetch_json(self.port, "/json/version"):
            return BackendState(chrome_up=False, logged_in=None, profile=str(self.profile))
        hosts = [urlsplit(p.get("url", "")).hostname or ""
                 for p in (await self._fetch_json(self.port, "/json/list") or []) if p.get("type") == "page"]
        if any(h in GROK_HOSTS for h in hosts):
            logged_in = True
        elif any(h.startswith(_LOGIN_PREFIX) for h in hosts):
            logged_in = False
        else:
            logged_in = None
        return BackendState(chrome_up=True, logged_in=logged_in, profile=str(self.profile))

    @asynccontextmanager
    async def _session(self):
        await self._ensure(self.port, self.profile, self.chrome_path)
        pages = await self._fetch_json(self.port, "/json/list") or []
        ws_url = pick_target(pages)
        if ws_url is None:
            created = await self._fetch_json(self.port, "/json/new?about:blank", method="PUT")
            ws_url = (created or {}).get("webSocketDebuggerUrl")
        if not ws_url:
            raise DriverError(f"в окне Chrome (порт {self.port}) нет вкладки, которой можно управлять")
        cdp = await self._connect(ws_url)
        try:
            await cdp.send("Page.bringToFront")
            yield GrokPage(cdp, profile=str(self.profile))
        finally:
            await cdp.close()

    async def generate(self, opts: VideoOptions | ImageOptions, reporter: Reporter) -> Generated:
        kind = "video" if isinstance(opts, VideoOptions) else "image"
        async with self._session() as page:
            await page.open(IMAGINE_URL)
            if kind == "video":
                await page.prepare_video(opts)
            else:
                await page.prepare_image(opts)
            await page.enter_prompt(opts.prompt)
            post_id = await page.submit()
            reporter.submitted(post_id)
            url = await page.wait_result(kind, post_id)
            reporter.phase("downloading")
            return await page.download(url, kind)

    async def resume(self, post_id: str, kind: str, reporter: Reporter) -> Generated:
        if not POST_ID_RE.match(post_id):
            raise DriverError("в записи задачи некорректный post_id: возобновить нельзя")
        async with self._session() as page:
            await page.open(POST_URL.format(post_id=post_id), post=True)
            url = await page.wait_result(kind, post_id)
            reporter.phase("downloading")
            return await page.download(url, kind)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_grok_backend.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/grok_backend.py tests/test_grok_backend.py
git commit -m "feat(grok-imagine-mcp): backend драйвера web: окно Chrome, CDP и страница Grok" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Подключение драйвера: `build_driver`, настройки, README, маркер live

**Files:**
- Modify: `src/grok_imagine_mcp/drivers.py` (`build_driver`, сообщение `_NO_KEY`, список доступных драйверов)
- Modify: `pyproject.toml` (маркер `live`)
- Modify: `README.md` (раздел «Драйвер web»)
- Modify: `tests/test_drivers.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `WebDriver` (Task 6), `GrokBackend` (Task 8), `find_chrome` (Task 4).
- Produces: `build_driver("web", state_dir, output_dir, env)` → `WebDriver`; неверное число в `GROK_WEB_PORT`, `GROK_WEB_GENERATION_TIMEOUT_S`, `GROK_WEB_QUEUE_TIMEOUT_S` → `DriverNotAvailable` (сервер печатает текст и завершается кодом 2, как для неизвестного драйвера).

- [ ] **Step 1: Write the failing test**

В `tests/test_drivers.py` заменить тест `test_build_driver_unknown_name` и добавить новые (импорт `WebDriver` делать внутри тестов, как в тесте `xai`):

```python
def test_build_driver_unknown_name(tmp_path):
    with pytest.raises(DriverNotAvailable, match="uia") as e:
        build_driver("uia", tmp_path, tmp_path / "out", env={})
    assert "web" in str(e.value)


def test_build_driver_web_defaults(tmp_path):
    from grok_imagine_mcp.web_driver import WebDriver
    drv = build_driver("web", tmp_path, tmp_path / "out", env={})
    assert isinstance(drv, WebDriver) and drv.name == "web"
    assert drv.lock_path == tmp_path / "web.lock"
    assert drv.backend.port == 9340 and drv.backend.profile == tmp_path / "chrome-profile"
    assert drv.queue_timeout_s == 900 and drv.generation_timeout_s == 900


def test_build_driver_web_reads_settings(tmp_path):
    drv = build_driver("web", tmp_path, tmp_path / "out", env={
        "GROK_WEB_PORT": "9555", "GROK_CHROME_PROFILE": str(tmp_path / "mine"),
        "GROK_WEB_GENERATION_TIMEOUT_S": "60", "GROK_WEB_QUEUE_TIMEOUT_S": "30"})
    assert drv.backend.port == 9555 and drv.backend.profile == tmp_path / "mine"
    assert drv.generation_timeout_s == 60 and drv.queue_timeout_s == 30


@pytest.mark.parametrize("name", ["GROK_WEB_PORT", "GROK_WEB_GENERATION_TIMEOUT_S", "GROK_WEB_QUEUE_TIMEOUT_S"])
def test_build_driver_web_rejects_a_non_number_with_the_setting_name(tmp_path, name):
    with pytest.raises(DriverNotAvailable, match=name):
        build_driver("web", tmp_path, tmp_path / "out", env={name: "много"})


def test_missing_key_hint_mentions_the_web_driver(tmp_path):
    drv = build_driver("xai", tmp_path, tmp_path / "out", env={})
    assert "GROK_DRIVER=web" in drv.reason
```

В `tests/test_server.py` добавить после `test_stdio_without_api_key_starts_and_explains`:

```python
def test_stdio_web_driver_reports_status_without_launching_chrome(tmp_path):
    """grok_status драйвера web только смотрит на порт: окно не запускает и страницы не открывает."""
    async def main():
        async with Client(_stdio(tmp_path, GROK_DRIVER="web", GROK_WEB_PORT="9")) as c:     # на порту 9 никого нет
            return await c.call_tool("grok_status", {})

    data = _payload(anyio.run(main))
    assert data["driver"] == "web" and data["live"] is True and data["ready"] is True
    assert "не запущено" in data["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_drivers.py tests\test_server.py -q`
Expected: FAIL (новые тесты: `DriverNotAvailable: Неизвестный драйвер 'web'`, `AttributeError`, отсутствие `GROK_DRIVER=web` в тексте).

- [ ] **Step 3: Write minimal implementation**

В `src/grok_imagine_mcp/drivers.py`:

1. Заменить константу `_NO_KEY`:

```python
_NO_KEY = ("XAI_API_KEY не задан. Создайте ключ на console.x.ai (API оплачивается отдельно от подписки SuperGrok) "
           "и передайте его серверу переменной окружения XAI_API_KEY. Без ключа, по подписке Grok, работает "
           "драйвер web: добавьте GROK_DRIVER=web в ~/.grok_imagine_mcp/.env.")
```

2. Добавить перед `build_driver` вспомогательную функцию и заменить тело `build_driver` (импорты `Path` в начало файла: `from pathlib import Path`):

```python
def _number(env: Mapping[str, str], name: str, default: float, cast=float):
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return cast(raw)
    except ValueError:
        raise DriverNotAvailable(f"{name} должен быть числом, получено {raw!r}") from None


def build_driver(name: str, state_dir, output_dir, env: Mapping[str, str]) -> Driver:
    store = TaskStore(state_dir / "tasks")
    if name == "dry-run":
        return DryRunDriver(store)
    if name == "xai":
        if not env.get("XAI_API_KEY"):
            return UnconfiguredDriver(_NO_KEY)
        from .xai_driver import XaiDriver, make_client
        key = env["XAI_API_KEY"]
        return XaiDriver(lambda: make_client(key), store, output_dir)
    if name == "web":
        from .chrome import find_chrome
        from .grok_backend import GrokBackend
        from .web_driver import WebDriver
        port = _number(env, "GROK_WEB_PORT", 9340, int)
        profile = Path(env.get("GROK_CHROME_PROFILE") or Path(state_dir) / "chrome-profile")
        backend = GrokBackend(port, profile, find_chrome(env))
        return WebDriver(backend, store, Path(output_dir), Path(state_dir) / "web.lock",
                         queue_timeout_s=_number(env, "GROK_WEB_QUEUE_TIMEOUT_S", 900.0),
                         generation_timeout_s=_number(env, "GROK_WEB_GENERATION_TIMEOUT_S", 900.0))
    raise DriverNotAvailable(f"Неизвестный драйвер {name!r}. Доступны: xai, dry-run, web")
```

В `pyproject.toml`, в секцию `[tool.pytest.ini_options]` добавить:

```toml
markers = ["live: нужен реальный Chrome с входом в Grok; включается GROK_WEB_LIVE=1"]
```

В `README.md` после раздела «Запасной путь: управление десктоп-приложением Grok» добавить раздел:

````markdown
## Драйвер web: Grok Imagine по подписке, без API-ключа

Драйвер `web` управляет страницей `grok.com/imagine` в отдельном окне Chrome. Это то же самое, что вы делаете руками,
только по команде из Claude Code или Codex: текст → видео, фото → видео, текст → картинка. Файлы сохраняются в
`GROK_OUTPUT_DIR`. Инструменты те же, что у драйвера `xai`.

**Включение.** Добавьте строку в `~/.grok_imagine_mcp/.env` (ключ не нужен):

```
GROK_DRIVER=web
```

**Первый вход.** При первой генерации сервер сам откроет окно Chrome с профилем `<GROK_STATE_DIR>\chrome-profile`.
Войдите в Grok в этом окне вручную (Google, X или почта) и повторите запрос. Сессия хранится в профиле. Сервер
никогда не вводит логин, пароль и коды и не принимает согласия на странице.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `GROK_WEB_PORT` | `9340` | Порт отладки окна Chrome (только `127.0.0.1`) |
| `GROK_CHROME_PROFILE` | `<GROK_STATE_DIR>\chrome-profile` | Профиль Chrome для Grok |
| `GROK_CHROME_PATH` | автопоиск | Путь к `chrome.exe` |
| `GROK_WEB_GENERATION_TIMEOUT_S` | `900` | Сколько ждать одну генерацию |
| `GROK_WEB_QUEUE_TIMEOUT_S` | `900` | Сколько ждать очередь к окну (Claude Code и Codex делят одно окно) |

**Что поддерживается.** Длительность 6, 10 или 15 с; разрешение 480p, 720p, 1080p (если позволяет ваша подписка);
формат 2:3, 3:2, 1:1, 9:16, 16:9; звук вкл/выкл. С фото формат берётся из фото, `aspect_ratio` игнорируется. Поле
`model` не применяется. Недопустимые значения отклоняются с перечнем допустимых.

**Риски.**
- Пока окно открыто, любая программа на этом компьютере может управлять вашей сессией Grok через порт отладки.
- Автоматизация веб-интерфейса подписки может не соответствовать условиям xAI.
- Смена вёрстки grok.com ломает драйвер. Подписи элементов русские, при другом языке интерфейса может потребоваться
  правка таблиц в `grok_page.py`.
- Генерации идут по одной, остальные ждут в очереди.

Живая проверка на вашем аккаунте (расходует лимит подписки): `$env:GROK_WEB_LIVE='1'; .\.venv\Scripts\pytest tests\test_web_live.py -s`.
````

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: все тесты проходят (по всему проекту; живой тест ещё не создан).

- [ ] **Step 5: Commit**

```bash
git add src/grok_imagine_mcp/drivers.py pyproject.toml README.md tests/test_drivers.py tests/test_server.py
git commit -m "feat(grok-imagine-mcp): подключение драйвера web: build_driver, настройки, README" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Живая проверка на реальном Chrome

**Files:**
- Create: `tests/test_web_live.py`

**Interfaces:**
- Consumes: `build_driver("web", …)` (Task 9), `check_mp4` (Task 2).
- Produces: два живых теста (видео по тексту, видео по синтетической картинке) и проверка картинки по тексту.

Этот шаг единственный, который **тратит лимит подписки** (три короткие генерации). Окно Chrome на порту 9340 с профилем `C:\Users\TLT-1\.grok_imagine_mcp\chrome-profile` уже запущено и залогинено после пробы; если окна нет, драйвер запустит его сам, а вход понадобится выполнить владельцу вручную.

- [ ] **Step 1: Write the live test**

Создать `tests/test_web_live.py`:

```python
"""Живой тест драйвера web: реальный Chrome, реальный аккаунт Grok, расходует лимит подписки.

Запуск:  $env:GROK_WEB_LIVE='1'; .\\.venv\\Scripts\\pytest tests\\test_web_live.py -s
"""
import asyncio
import os
import struct
import zlib
from pathlib import Path

import pytest

from grok_imagine_mcp.drivers import build_driver
from grok_imagine_mcp.media_check import check_mp4, detect_image_ext
from grok_imagine_mcp.models import ImageRequest, VideoRequest

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("GROK_WEB_LIVE") != "1", reason="живой тест: задайте GROK_WEB_LIVE=1")]


def _png(path: Path, w=512, h=768) -> None:
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            rows += bytes((250, 210, 60) if (x - 256) ** 2 + (y - 240) ** 2 < 70 ** 2
                          else (60, 140, 70) if y > 560 else (90 + y // 12, 150 + y // 16, 235 - y // 10))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def _driver(tmp_path):
    home = Path(os.environ.get("GROK_STATE_DIR") or Path.home() / ".grok_imagine_mcp")
    env = {**os.environ, "GROK_CHROME_PROFILE": os.environ.get("GROK_CHROME_PROFILE") or str(home / "chrome-profile")}
    return build_driver("web", tmp_path, tmp_path / "out", env)           # задачи и блокировка во временной папке


async def _until_done(drv, task_id, limit_s=600):
    waited = 0
    while waited < limit_s:
        info = await drv.task_status(task_id, 45)
        if info.status != "pending":
            return info
        waited += 45
    raise AssertionError("генерация не завершилась за отведённое время")


def test_live_text_to_video(tmp_path):
    async def main():
        drv = _driver(tmp_path)
        info = await drv.submit_video(VideoRequest(prompt="A red paper boat floating on calm water, soft daylight",
                                                   duration_s=6, resolution="480p"))
        return await _until_done(drv, info.task_id)
    info = asyncio.run(main())
    assert info.status == "done", info.error
    check_mp4(Path(info.output_path).read_bytes())


def test_live_photo_to_video(tmp_path):
    img = tmp_path / "start.png"
    _png(img)

    async def main():
        drv = _driver(tmp_path)
        info = await drv.submit_video(VideoRequest(prompt="The sun slowly rises, gentle camera push-in",
                                                   image_path=str(img), duration_s=6, resolution="480p"))
        return await _until_done(drv, info.task_id)
    info = asyncio.run(main())
    assert info.status == "done", info.error
    check_mp4(Path(info.output_path).read_bytes())


def test_live_text_to_image(tmp_path):
    async def main():
        drv = _driver(tmp_path)
        info = await drv.submit_image(ImageRequest(prompt="A small red paper boat on calm water, soft daylight"))
        return await _until_done(drv, info.task_id, limit_s=300)
    info = asyncio.run(main())
    assert info.status == "done", info.error
    detect_image_ext(Path(info.output_path).read_bytes())
```

- [ ] **Step 2: Run the ordinary suite, the live test must be skipped**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: все обычные тесты проходят, три живых `skipped`.

- [ ] **Step 3: Run the live test (с согласия владельца)**

Перед запуском напомнить владельцу, что будут три короткие генерации (две 6-секундные 480p и одна картинка), и дождаться «да», если оно не было дано при одобрении плана.

Run:
```powershell
$env:GROK_WEB_LIVE = '1'
.\.venv\Scripts\python.exe -m pytest tests\test_web_live.py -s -x
```
Expected: `3 passed`. Окно Chrome на порту 9340 остаётся открытым.

- [ ] **Step 4: Если живой тест упал, диагностика (не гадать)**

Для каждого падения выполнить по порядку:

1. Прочитать текст `DriverError`. Ошибки вида «не найден элемент «…». Подписи на странице: …» уже содержат список найденных подписей: поправить таблицу `L_*` в `grok_page.py`.
2. Для падения на этапе результата (`не показал ни прогресса, ни результата`, `не дождались…`) снять живое состояние страницы тем же кодом, что использует драйвер. Создать одноразовый скрипт `probe_state.py` в scratchpad (не в репозитории):

```python
import asyncio, json, sys
from grok_imagine_mcp.cdp import CdpConnection, http_json
from grok_imagine_mcp.grok_backend import pick_target
from grok_imagine_mcp.grok_page import GrokPage

async def main():
    pages = await http_json(9340, "/json/list")
    cdp = await CdpConnection.connect(pick_target(pages))
    try:
        page = GrokPage(cdp)
        print(json.dumps(await page._state(sys.argv[1] if len(sys.argv) > 1 else ""), ensure_ascii=False, indent=1))
    finally:
        await cdp.close()

asyncio.run(main())
```

   Запуск на странице результата: `.\.venv\Scripts\python.exe probe_state.py <post_id>`. Смотреть поля `progress`, `vids`, `imgs`, `dialog`, `text`.
3. Режим «Изображение» в спецификации не проверялся пробой: если `test_live_text_to_image` упал, а видео-тесты прошли, причина почти наверняка в признаке результата картинки (`imgs` в `_STATE`: порог `naturalWidth >= 256` и наличие `post_id` в `src`) или в формате/подписях режима. Исправить селектор в `_STATE` по выводу `probe_state.py`, прогнать `tests\test_grok_page.py` и повторить живой тест. Если картинка получается из другого адреса, не содержащего `post_id`, фильтр по `post_id` для картинок заменить на фильтр по хосту `assets.grok.com`.
4. После правки вёрстки-зависимых констант обязательно прогнать `.\.venv\Scripts\python.exe -m pytest -q` (обычные тесты) и повторить живой тест.

- [ ] **Step 5: Commit**

```bash
git add tests/test_web_live.py src/grok_imagine_mcp/grok_page.py
git commit -m "test(grok-imagine-mcp): живой тест драйвера web (видео по тексту и по фото, картинка)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```
(`grok_page.py` добавляется, только если в Step 4 его правили.)

---

### Task 11: Включение у владельца и итоговая проверка

**Files:**
- Modify (вне репозитория, только с согласия владельца): `C:\Users\TLT-1\.grok_imagine_mcp\.env`

**Interfaces:**
- Consumes: всё выше. Ничего нового не производит.

- [ ] **Step 1: Полный прогон тестов**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: все обычные тесты проходят, живые `skipped`. Записать итоговое число в отчёт владельцу.

- [ ] **Step 2: Спросить владельца про `.env`**

Файл `~/.grok_imagine_mcp/.env` принадлежит владельцу (в нём может быть `XAI_API_KEY`). Спросить, добавить ли в него строку `GROK_DRIVER=web`. Ключ не читать и не выводить. Если владелец согласен:

```powershell
$env_file = "$HOME\.grok_imagine_mcp\.env"
if (-not (Test-Path $env_file) -or -not (Select-String -Path $env_file -Pattern '^\s*GROK_DRIVER\s*=' -Quiet)) {
    Add-Content -Path $env_file -Value "GROK_DRIVER=web" -Encoding UTF8
}
```
Команда только добавляет строку и ничего не выводит из файла. Если строка `GROK_DRIVER=` там уже есть, не менять её, а сообщить владельцу.

- [ ] **Step 3: Сквозная проверка через настоящего клиента**

Сервера уже зарегистрированы (`grok-imagine-xai` в Claude Code, `grok-imagine` в Codex) и подхватят `.env` при следующем запуске. Попросить владельца перезапустить Claude Code и Codex, затем из Claude Code вызвать `grok_status` (ожидается `driver: web`, `ready: true`) и одну короткую генерацию `grok_generate_video` с `duration_s=6`, `resolution="480p"`, затем `grok_task_status` с `wait_s=45` до `done`. Результат (путь к mp4) показать владельцу.

- [ ] **Step 4: Отчёт и состояние репозитория**

Run: `git log --oneline -12` и `git status --short`
Expected: список коммитов этой работы, рабочее дерево чистое. `git push` не выполнять: решение о публикации ветки за владельцем. Сообщить владельцу: что сделано, что осталось открытым (если живой тест что-то выявил), и что окно Chrome на порту 9340 остаётся открытым, а тестовые генерации видны в его истории Grok.
