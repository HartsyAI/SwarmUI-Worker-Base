"""Authenticated reverse proxy in front of a loopback-only SwarmUI.

SwarmUI treats every loopback caller as its local admin, and cloud providers publish a worker's port
on the open internet (RunPod's proxy docs say plainly to put auth in the application). So nothing
reaches SwarmUI unless it carries the current worker token:

- `Authorization: Bearer <token>`: what core SwarmUI's swarm backend sends, on HTTP and WebSocket
  alike (its `AuthorizationHeader` setting).
- An HttpOnly cookie, set only by the optional one-time `?worker_token=` browser login.

With no token set (between leases), every request is refused. Rotating the token closes every open
WebSocket, so whoever held the previous lease is cut off at once.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Mapping, Optional
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from . import logs

log = logging.getLogger("swarmui_worker.gateway")

COOKIE_NAME = "swarmui_worker_token"
URL_LOGIN_PARAM = "worker_token"

# Never forwarded upstream: hop-by-hop headers, plus our own credentials (SwarmUI never needs them and
# must not log them) and client-supplied forwarding headers (SwarmUI trusts the socket address only).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "host", "content-length",
}
_STRIPPED_REQUEST = _HOP_BY_HOP | {"authorization", "forwarded", "x-forwarded-for", "x-forwarded-host",
                                   "x-forwarded-proto", "x-real-ip"}
# Bodies pass through undecoded (auto_decompress=False), so Content-Encoding is kept as-is.
_STRIPPED_RESPONSE = _HOP_BY_HOP


def _tokens_match(given: Optional[str], expected: Optional[str]) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _forward_headers(headers: Mapping[str, str], stripped: set[str]) -> CIMultiDict:
    # A multidict, so repeated headers (e.g. several Set-Cookie) all survive.
    return CIMultiDict((k, v) for k, v in headers.items() if k.lower() not in stripped)


def _without_worker_cookie(headers: CIMultiDict) -> CIMultiDict:
    """Drops only our own cookie; SwarmUI's cookies pass through untouched."""
    cookies = headers.getall("Cookie", [])
    if not cookies:
        return headers
    del headers["Cookie"]
    kept = []
    for line in cookies:
        for part in line.split(";"):
            if part.strip() and part.split("=", 1)[0].strip() != COOKIE_NAME:
                kept.append(part.strip())
    if kept:
        headers["Cookie"] = "; ".join(kept)
    return headers


def _is_https(request: web.Request) -> bool:
    # TLS usually ends at the provider's proxy, which says so in X-Forwarded-Proto.
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


class Gateway:
    """The public-facing proxy. One per worker process."""

    def __init__(self, upstream_port: int, allow_url_login: bool = False):
        self._upstream = f"http://127.0.0.1:{upstream_port}"
        self._allow_url_login = allow_url_login
        self._token: Optional[str] = None
        self._upstream_ready = False
        self._sockets: set[web.WebSocketResponse] = set()
        self._client: Optional[aiohttp.ClientSession] = None
        self._runner: Optional[web.AppRunner] = None

    # ── Token lifecycle ─────────────────────────────────────────────────────

    @property
    def has_token(self) -> bool:
        """True while a lease (or a fixed token) is active."""
        return self._token is not None

    async def set_token(self, token: Optional[str]) -> None:
        """Replaces the accepted token (None refuses everything) and drops every open WebSocket."""
        old = self._token
        self._token = token
        if token:
            logs.register_secret(token)
        if old and old != token:
            await self.close_sockets()
            logs.forget_secret(old)

    def set_upstream_ready(self, ready: bool) -> None:
        """Until SwarmUI answers, authenticated requests get a clean 503 instead of a connect error."""
        self._upstream_ready = ready

    async def close_sockets(self) -> None:
        """Closes every proxied WebSocket, e.g. when a lease ends."""
        for ws in list(self._sockets):
            await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"lease ended")
        self._sockets.clear()

    # ── Server lifecycle ────────────────────────────────────────────────────

    def make_app(self) -> web.Application:
        """Builds the aiohttp application (exposed for tests)."""
        app = web.Application(client_max_size=0)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def start(self, host: str, port: int) -> None:
        """Starts listening."""
        self._runner = web.AppRunner(self.make_app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        log.info("Gateway listening on %s:%d", host, port)

    async def stop(self) -> None:
        """Stops listening and closes every connection."""
        await self.close_sockets()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _on_startup(self, app: web.Application) -> None:
        # No total timeout: a generation can legitimately run for many minutes.
        self._client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=10),
                                             auto_decompress=False)

    async def _on_cleanup(self, app: web.Application) -> None:
        if self._client is not None:
            await self._client.close()

    # ── Request handling ────────────────────────────────────────────────────

    def _presented_token(self, request: web.Request) -> Optional[str]:
        auth = request.headers.get("Authorization", "")
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        return request.cookies.get(COOKIE_NAME)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        started = time.monotonic()
        response = await self._dispatch(request)
        # Path only: the query string may carry a login token.
        log.debug("%s %s -> %s in %.0fms", request.method, request.path, response.status,
                  (time.monotonic() - started) * 1000)
        return response

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        if self._allow_url_login and URL_LOGIN_PARAM in request.query:
            return self._url_login(request)
        if not _tokens_match(self._presented_token(request), self._token):
            return web.json_response({"error": "unauthorized", "error_id": "worker_unauthorized"}, status=401,
                                     headers={"WWW-Authenticate": 'Bearer realm="swarmui-worker"'})
        if not self._upstream_ready:
            return web.json_response({"error": "SwarmUI is still starting", "error_id": "worker_starting"},
                                     status=503, headers={"Retry-After": "5"})
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_websocket(request)
        return await self._proxy_http(request)

    def _url_login(self, request: web.Request) -> web.StreamResponse:
        if not _tokens_match(request.query.get(URL_LOGIN_PARAM), self._token):
            return web.json_response({"error": "unauthorized", "error_id": "worker_unauthorized"}, status=401)
        rest = {k: v for k, v in request.query.items() if k != URL_LOGIN_PARAM}
        location = request.path + (f"?{urlencode(rest)}" if rest else "")
        response = web.Response(status=303, headers={"Location": location})
        response.set_cookie(COOKIE_NAME, self._token, httponly=True, secure=_is_https(request), samesite="Strict",
                            path="/")
        return response

    async def _proxy_http(self, request: web.Request) -> web.StreamResponse:
        assert self._client is not None
        url = self._upstream + request.rel_url.path_qs
        headers = _without_worker_cookie(_forward_headers(request.headers, _STRIPPED_REQUEST))
        data = request.content if request.body_exists else None
        try:
            async with self._client.request(request.method, url, headers=headers, data=data,
                                            allow_redirects=False) as upstream:
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                              headers=_forward_headers(upstream.headers, _STRIPPED_RESPONSE))
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except aiohttp.ClientConnectionError as ex:
            log.warning("Upstream SwarmUI unreachable for %s: %s", request.path, ex)
            return web.json_response({"error": "SwarmUI is not reachable", "error_id": "worker_upstream_down"},
                                     status=502)

    async def _proxy_websocket(self, request: web.Request) -> web.StreamResponse:
        assert self._client is not None
        client_ws = web.WebSocketResponse(max_msg_size=0, autoping=True)
        await client_ws.prepare(request)
        self._sockets.add(client_ws)
        url = self._upstream.replace("http://", "ws://") + request.rel_url.path_qs
        headers = _without_worker_cookie(_forward_headers(request.headers, _STRIPPED_REQUEST | {
            "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}))
        try:
            async with self._client.ws_connect(url, headers=headers, max_msg_size=0, autoping=True) as upstream_ws:
                async def pump(source, sink) -> None:
                    async for msg in source:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await sink.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await sink.send_bytes(msg.data)
                        else:
                            break
                tasks = [asyncio.create_task(pump(client_ws, upstream_ws)),
                         asyncio.create_task(pump(upstream_ws, client_ws))]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
        except aiohttp.ClientError as ex:
            log.warning("Upstream WebSocket failed for %s: %s", request.path, ex)
        finally:
            self._sockets.discard(client_ws)
            if not client_ws.closed:
                await client_ws.close()
        return client_ws
