"""Web-процес: REST/JSON API + WebSocket + статичний Web UI (aiohttp).

Цей процес НЕ торкається пристроїв: він лише проксіює команди до демона через
control-канал (ipc/client.py) та розсилає живі події браузерам. Тому збій чи
перезапуск Web UI ніколи не зачіпає гарячий шлях введення, а демон працює і без
Web-процесу.

Залежність: ``pip install aiohttp`` (extra «web»).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import WSMsgType, web

from ..config import DaemonConfig, read_or_create_token
from ..ipc import protocol
from ..ipc.client import ControlClient, ControlError
from .security import origin_allowed

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
WS_QUEUE = 128          # кадрів у черзі одного браузера; «state» понад це відкидаються

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_HTTP_BY_CODE = {"param": 400, "profile": 400, "layout": 400, "auth": 401, "device": 409,
                 "store": 400, "unavailable": 503, "timeout": 504, "internal": 500}


# ===========================================================================
# MIDDLEWARE
# ===========================================================================
@web.middleware
async def guard(request: web.Request, handler):
    """Origin/Host-перевірка для ВСІХ запитів + безпечні заголовки відповіді."""
    cfg: DaemonConfig = request.app["cfg"]
    if not origin_allowed(request.headers.get("Host"), request.headers.get("Origin"),
                          cfg.allowed_hosts()):
        return web.json_response({"error": "forbidden origin/host"}, status=403)
    resp = await handler(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    if not isinstance(resp, web.WebSocketResponse):
        resp.headers["Content-Security-Policy"] = CSP
    return resp


@web.middleware
async def errors(request: web.Request, handler):
    """ControlError демона -> HTTP-статус + JSON {"error": "..."}."""
    try:
        return await handler(request)
    except ControlError as e:
        status = _HTTP_BY_CODE.get(e.code, 500)
        if "не знайдено" in str(e) or "не відкрито" in str(e):
            status = 404
        return web.json_response({"error": str(e), "code": e.code}, status=status)


async def _body(request: web.Request) -> Any:
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "тіло має бути JSON"}),
                                 content_type="application/json") from None


def _rpc(request: web.Request):
    return request.app["client"].call


# ===========================================================================
# WEBSOCKET-ХАБ
# ===========================================================================
class WsHub:
    """Розсилає події демона всім браузерам. Кожен клієнт має власну обмежену
    чергу й власну задачу-відправник: повільний браузер не гальмує інших, а його
    застарілі кадри `state` просто відкидаються (UI потрібен лише останній)."""

    def __init__(self) -> None:
        self.clients: Dict[web.WebSocketResponse, asyncio.Queue] = {}

    def broadcast(self, event: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        droppable = event.get("event") == "state"
        for q in self.clients.values():
            if q.full():
                if droppable:
                    continue
                try:
                    q.get_nowait()                 # звільняємо місце для важливої події
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(text)

    async def serve(self, ws: web.WebSocketResponse, hello: list) -> None:
        q: asyncio.Queue = asyncio.Queue(WS_QUEUE)
        self.clients[ws] = q
        for ev in hello:
            q.put_nowait(json.dumps(ev, ensure_ascii=False))

        async def pump() -> None:
            try:
                while True:
                    await ws.send_str(await q.get())
            except (ConnectionResetError, RuntimeError):
                pass                               # браузер закрив з'єднання

        sender = asyncio.create_task(pump())
        try:
            async for msg in ws:                   # від браузера нічого не потрібно, лише ping/close
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            sender.cancel()
            self.clients.pop(ws, None)


# ===========================================================================
# МАРШРУТИ
# ===========================================================================
routes = web.RouteTableDef()


@routes.get("/")
async def index(request: web.Request):
    return web.FileResponse(STATIC_DIR / "index.html")


@routes.get("/api/health")
async def health(request: web.Request):
    return web.json_response({"web": True, "daemon": request.app["client"].connected.is_set()})


@routes.get("/api/devices")
async def devices(request):
    return web.json_response(await _rpc(request)("devices"))


@routes.post("/api/rescan")
async def rescan(request):
    return web.json_response(await _rpc(request)("rescan"))


@routes.post("/api/devices/{dev}/open")
async def dev_open(request):
    body = await _body(request) if request.can_read_body else {}
    return web.json_response(await _rpc(request)("open", device=request.match_info["dev"],
                                                 layout=(body or {}).get("layout")))


@routes.post("/api/devices/{dev}/close")
async def dev_close(request):
    return web.json_response(await _rpc(request)("close", device=request.match_info["dev"]))


@routes.get("/api/devices/{dev}/profile")
async def dev_profile(request):
    return web.json_response(await _rpc(request)("active_profile", device=request.match_info["dev"]))


@routes.put("/api/devices/{dev}/profile")
async def dev_activate(request):
    body = await _body(request)
    return web.json_response(await _rpc(request)("activate", device=request.match_info["dev"],
                                                 name=body.get("name")))


@routes.patch("/api/devices/{dev}/profile")
async def dev_patch(request):
    """Гаряча зміна налаштувань: тіло — JSON merge-patch (RFC 7386); ?save=1 — ще й на диск."""
    patch = await _body(request)
    save = request.query.get("save") in ("1", "true")
    return web.json_response(await _rpc(request)("patch_profile", device=request.match_info["dev"],
                                                 patch=patch, save=save))


@routes.get("/api/devices/{dev}/stats")
async def dev_stats(request):
    return web.json_response(await _rpc(request)("stats", device=request.match_info["dev"]))


@routes.post("/api/devices/{dev}/stats/reset")
async def dev_stats_reset(request):
    return web.json_response(await _rpc(request)("stats_reset", device=request.match_info["dev"]))


@routes.get("/api/devices/{dev}/sniff")
async def sniff_get(request):
    return web.json_response(await _rpc(request)("sniff", device=request.match_info["dev"],
                                                 action="summary"))


@routes.post("/api/devices/{dev}/sniff")
async def sniff_post(request):
    b = await _body(request)
    return web.json_response(await _rpc(request)("sniff", device=request.match_info["dev"],
                                                 action=b.get("action", "summary"),
                                                 name=b.get("name"), baseline_s=b.get("baseline_s")))


@routes.get("/api/profiles")
async def profiles(request):
    return web.json_response(await _rpc(request)("profiles"))


@routes.get("/api/profiles/{name}")
async def profile_get(request):
    return web.json_response(await _rpc(request)("profile_get", name=request.match_info["name"]))


@routes.put("/api/profiles/{name}")
async def profile_put(request):
    return web.json_response(await _rpc(request)("profile_save", name=request.match_info["name"],
                                                 data=await _body(request)))


@routes.delete("/api/profiles/{name}")
async def profile_delete(request):
    return web.json_response(await _rpc(request)("profile_delete", name=request.match_info["name"]))


@routes.get("/api/layouts")
async def layouts(request):
    return web.json_response(await _rpc(request)("layouts"))


@routes.get("/api/layouts/{id}")
async def layout_get(request):
    return web.json_response(await _rpc(request)("layout", id=request.match_info["id"]))


@routes.get("/ws")
async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
    await ws.prepare(request)
    client: ControlClient = request.app["client"]
    hello = [{"event": "daemon", "connected": client.connected.is_set()}]
    if client.connected.is_set():
        try:
            hello.append({"event": "devices", "devices": await client.call("devices")})
        except ControlError:
            pass
    await request.app["hub"].serve(ws, hello)
    return ws


# ===========================================================================
# ЗБІРКА ЗАСТОСУНКУ
# ===========================================================================
def create_app(cfg: DaemonConfig) -> web.Application:
    app = web.Application(middlewares=[guard, errors], client_max_size=1 << 20)
    app["cfg"] = cfg
    hub = WsHub()
    app["hub"] = hub
    token = read_or_create_token(cfg.token_path) if cfg.endpoint().startswith("tcp:") else None

    def on_connection(up: bool) -> None:
        hub.broadcast({"event": "daemon", "connected": up})

    app["client"] = ControlClient(cfg.endpoint(), token, on_event=hub.broadcast,
                                  on_connection=on_connection,
                                  subscribe=["state", "devices", "profile", "stats"])

    async def startup(app: web.Application) -> None:
        app["client"].start()

    async def cleanup(app: web.Application) -> None:
        await app["client"].close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app


def run_web(cfg: DaemonConfig) -> None:
    """Точка входу Web-процесу (викликається з CLI або multiprocessing.Process)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s web %(levelname)s %(message)s")
    log.info("Web UI: http://%s:%d/", cfg.web_host, cfg.web_port)
    web.run_app(create_app(cfg), host=cfg.web_host, port=cfg.web_port,
                access_log=None, print=None, handle_signals=True)
