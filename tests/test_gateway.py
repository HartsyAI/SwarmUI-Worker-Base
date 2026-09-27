"""Gateway behavior against a fake upstream SwarmUI, over real sockets."""

from __future__ import annotations

import asyncio
import socket

import aiohttp
from aiohttp import web

from swarmui_worker import logs
from swarmui_worker.gateway import COOKIE_NAME, Gateway

TOKEN = "t" * 48


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_upstream(port: int, seen: list) -> web.AppRunner:
    async def echo(request: web.Request) -> web.StreamResponse:
        seen.append(dict(request.headers))
        body = await request.read()
        resp = web.json_response({"path": request.path_qs, "method": request.method, "body": body.decode()})
        resp.headers.add("Set-Cookie", "a=1")
        resp.headers.add("Set-Cookie", "b=2")
        return resp

    async def ws(request: web.Request) -> web.WebSocketResponse:
        seen.append(dict(request.headers))
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        async for msg in sock:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await sock.send_str("echo:" + msg.data)
        return sock

    app = web.Application()
    app.router.add_get("/API/ws", ws)
    app.router.add_route("*", "/{tail:.*}", echo)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


class Harness:
    def __init__(self, allow_url_login: bool = False):
        self.upstream_port = _free_port()
        self.public_port = _free_port()
        self.seen: list = []
        self.gateway = Gateway(self.upstream_port, allow_url_login=allow_url_login)
        self.upstream = None

    async def __aenter__(self) -> "Harness":
        self.upstream = await _start_upstream(self.upstream_port, self.seen)
        await self.gateway.start("127.0.0.1", self.public_port)
        self.gateway.set_upstream_ready(True)
        await self.gateway.set_token(TOKEN)
        self.base = f"http://127.0.0.1:{self.public_port}"
        return self

    async def __aexit__(self, *exc) -> None:
        await self.gateway.stop()
        await self.upstream.cleanup()


def run(coro):
    return asyncio.run(coro)


def test_rejects_missing_and_wrong_token():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            async with c.get(h.base + "/API/GetNewSession") as r:
                assert r.status == 401
                assert (await r.json())["error_id"] == "worker_unauthorized"
            async with c.get(h.base + "/", headers={"Authorization": "Bearer nope"}) as r:
                assert r.status == 401
            assert h.seen == []
    run(body())


def test_proxies_with_token_and_strips_credentials_and_forwarding_headers():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            headers = {"Authorization": f"Bearer {TOKEN}", "X-Forwarded-For": "1.2.3.4",
                       "Cookie": f"{COOKIE_NAME}={TOKEN}; keep=me"}
            async with c.post(h.base + "/API/Thing?x=1", data="hello", headers=headers) as r:
                assert r.status == 200
                data = await r.json()
                assert data == {"path": "/API/Thing?x=1", "method": "POST", "body": "hello"}
                assert r.headers.getall("Set-Cookie") == ["a=1", "b=2"]
            upstream_headers = {k.lower(): v for k, v in h.seen[0].items()}
            assert "authorization" not in upstream_headers
            assert "x-forwarded-for" not in upstream_headers
            assert upstream_headers["cookie"] == "keep=me"
            assert TOKEN not in str(h.seen)
    run(body())


def test_no_token_refuses_everything():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            await h.gateway.set_token(None)
            async with c.get(h.base + "/", headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                assert r.status == 401
    run(body())


def test_starting_returns_503():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            h.gateway.set_upstream_ready(False)
            async with c.get(h.base + "/", headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                assert r.status == 503
                assert r.headers["Retry-After"] == "5"
    run(body())


def test_websocket_requires_token_and_proxies():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            try:
                async with c.ws_connect(h.base + "/API/ws"):
                    raise AssertionError("unauthenticated websocket was accepted")
            except aiohttp.WSServerHandshakeError as ex:
                assert ex.status == 401
            async with c.ws_connect(h.base + "/API/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
                await ws.send_str("hi")
                msg = await ws.receive(timeout=5)
                assert msg.data == "echo:hi"
            ws_headers = {k.lower() for k in h.seen[-1]}
            assert "authorization" not in ws_headers
    run(body())


def test_rotating_token_closes_open_websockets():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            async with c.ws_connect(h.base + "/API/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
                await h.gateway.set_token("n" * 48)
                msg = await ws.receive(timeout=5)
                assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
            async with c.get(h.base + "/", headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                assert r.status == 401
    run(body())


def test_url_login_is_off_by_default():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            async with c.get(h.base + f"/?worker_token={TOKEN}", allow_redirects=False) as r:
                assert r.status == 401
    run(body())


def test_url_login_sets_cookie_and_strips_token_from_redirect():
    async def body():
        async with Harness(allow_url_login=True) as h, aiohttp.ClientSession() as c:
            async with c.get(h.base + f"/Text2Image?worker_token={TOKEN}&x=1", allow_redirects=False) as r:
                assert r.status == 303
                assert r.headers["Location"] == "/Text2Image?x=1"
                cookie = r.cookies[COOKIE_NAME]
                assert cookie.value == TOKEN
                assert cookie["httponly"]
            async with c.get(h.base + "/Text2Image", cookies={COOKIE_NAME: TOKEN}) as r:
                assert r.status == 200
    run(body())


def test_token_is_redacted_from_logs():
    async def body():
        async with Harness():
            assert logs.redact(f"header was Bearer {TOKEN}") == f"header was Bearer {logs.REDACTED}"
    run(body())
