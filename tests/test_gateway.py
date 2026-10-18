"""Gateway behavior against a fake upstream SwarmUI, over real sockets."""

from __future__ import annotations

import asyncio
import socket

import aiohttp
from aiohttp import web

from swarmui_worker import logs
from swarmui_worker.gateway import COOKIE_NAME, Gateway
from yarl import URL as yarl_url_cls


def yarl_url(url: str) -> yarl_url_cls:
    return yarl_url_cls(url, encoded=True)

TOKEN = "t" * 48


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_upstream(port: int, seen: list) -> web.AppRunner:
    async def echo(request: web.Request) -> web.StreamResponse:
        seen.append(dict(request.headers))
        # Like SwarmUI's API: a body must come with an exact Content-Length.
        if request.method == "POST" and request.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return web.json_response({"error": "Request has invalid content length"}, status=400)
        body = await request.read()
        resp = web.json_response({"path": request.path_qs, "raw": request.raw_path, "method": request.method,
                                  "body": body.decode()})
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

    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(3)
        return web.Response(text="late")

    app = web.Application()
    app.router.add_get("/API/ws", ws)
    app.router.add_get("/slow", slow)
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
            async def body_stream():
                yield b"hel"
                yield b"lo"
            # A chunked upload from the client must still reach SwarmUI with a Content-Length.
            async with c.post(h.base + "/API/Thing?x=1", data=body_stream(), headers=headers) as r:
                assert r.status == 200
                data = await r.json()
                assert data == {"path": "/API/Thing?x=1", "raw": "/API/Thing?x=1", "method": "POST", "body": "hello"}
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


def test_tls_gateway(tmp_path):
    import shutil
    import ssl
    import subprocess
    import pytest
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)

    async def body():
        h = Harness()
        h.upstream = await _start_upstream(h.upstream_port, h.seen)
        await h.gateway.start("127.0.0.1", h.public_port, str(cert), str(key))
        h.gateway.set_upstream_ready(True)
        await h.gateway.set_token(TOKEN)
        try:
            trusted = ssl.create_default_context(cafile=str(cert))
            async with aiohttp.ClientSession() as c:
                async with c.get(f"https://127.0.0.1:{h.public_port}/x", ssl=trusted,
                                 headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                    assert r.status == 200
                try:
                    async with c.get(f"https://127.0.0.1:{h.public_port}/x"):
                        raise AssertionError("untrusted certificate was accepted")
                except aiohttp.ClientConnectorCertificateError:
                    pass
        finally:
            await h.gateway.stop()
            await h.upstream.cleanup()
    run(body())


def test_no_compression_unless_the_client_asks():
    async def body():
        async with Harness() as h, aiohttp.ClientSession(skip_auto_headers=["Accept-Encoding"]) as c:
            async with c.get(h.base + "/x", headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                assert r.status == 200
            assert h.seen[-1].get("Accept-Encoding") == "identity"
            async with c.get(h.base + "/x", headers={"Authorization": f"Bearer {TOKEN}", "Accept-Encoding": "gzip"}) as r:
                assert r.status == 200
            assert h.seen[-1].get("Accept-Encoding") == "gzip"
    run(body())


def test_encoded_paths_reach_upstream_unchanged():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            url = h.base + "/View/a%3Fb%23c%25d.png?x=1"
            async with c.get(yarl_url(url), headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                assert (await r.json())["raw"] == "/View/a%3Fb%23c%25d.png?x=1"
    run(body())


def test_login_token_is_never_forwarded_upstream():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            async with c.get(h.base + f"/x?worker_token={TOKEN}&y=2", headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                data = await r.json()
                assert TOKEN not in data["raw"] and data["raw"] == "/x?y=2"
    run(body())


def test_rotating_token_cancels_in_flight_requests():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            async def slow():
                async with c.get(h.base + "/slow", headers={"Authorization": f"Bearer {TOKEN}"}) as r:
                    return await r.read()
            pending = asyncio.create_task(slow())
            await asyncio.sleep(0.3)
            await h.gateway.set_token(None)
            try:
                result = await asyncio.wait_for(pending, 2)
            except (aiohttp.ClientError, asyncio.TimeoutError) as ex:
                result = ex
            # Cut off before the upstream answers (the client may retry and then get a 401).
            assert not isinstance(result, asyncio.TimeoutError) and result != b"late"
    run(body())


def test_token_rotation_during_websocket_handshake_closes_it():
    async def body():
        async with Harness() as h, aiohttp.ClientSession() as c:
            original = web.WebSocketResponse.prepare

            async def rotating_prepare(self, request):
                result = await original(self, request)
                await h.gateway.set_token(None)
                return result
            web.WebSocketResponse.prepare = rotating_prepare
            try:
                async with c.ws_connect(h.base + "/API/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
                    msg = await asyncio.wait_for(ws.receive(), 5)
                    assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
            finally:
                web.WebSocketResponse.prepare = original
            await asyncio.sleep(0.2)
            assert not h.gateway._sockets
    run(body())
