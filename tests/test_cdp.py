import asyncio
import json

import pytest
from aiohttp import web

from grok_imagine_mcp.cdp import CdpConnection, CdpConnectionLost, CdpError, http_json


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


def test_closed_connection_fails_pending_command_with_a_distinct_error():
    async def body(conn):
        with pytest.raises(CdpConnectionLost, match="закрыто"):
            await conn.send("Hang")
        with pytest.raises(CdpConnectionLost):                   # и следующая команда тоже
            await conn.send("Echo")
    _run(body)


def test_protocol_errors_are_not_connection_loss():
    assert not issubclass(CdpError, CdpConnectionLost) and issubclass(CdpConnectionLost, CdpError)


def test_connecting_to_a_dead_port_is_connection_loss():
    with pytest.raises(CdpConnectionLost):
        asyncio.run(CdpConnection.connect("ws://127.0.0.1:1/devtools/page/x"))


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
