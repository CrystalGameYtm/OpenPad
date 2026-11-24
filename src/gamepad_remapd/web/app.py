"""Web process: REST/JSON API + WebSocket + the static Web UI (aiohttp).

This process does NOT touch the devices: it only proxies commands to the daemon
through the control channel (ipc/client.py) and broadcasts live events to browsers. So a failure or
restart of the Web UI never affects the input hot path, and the daemon runs without the Web process.

Dependency: ``pip install aiohttp`` (extra "web").
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import WSMsgType, web

from ..config import DaemonConfig, read_or_create_token
from ..ipc import protocol
from ..logic.protocol import render_protocol
from ..ipc.client import ControlClient, ControlError
from .locales import list_locales, load_locale
from .security import SESSION_COOKIE, authorized, origin_allowed, token_matches
from ..i18n import tr

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
WS_QUEUE = 128          

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_HTTP_BY_CODE = {"param": 400, "profile": 400, "layout": 400, "auth": 401, "device": 409, "not_found": 404,
                 "store": 400, "unavailable": 503, "timeout": 504, "internal": 500}


# ===========================================================================
# MIDDLEWARE
# ===========================================================================
@web.middleware
async def guard(request: web.Request, handler):
    """Origin/Host check for ALL requests + safe response headers."""
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


_LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>Gamepad Remapper</title>
<style>body{{font:16px system-ui;max-width:42rem;margin:4rem auto;padding:0 1rem;line-height:1.5}}code{{background:#8883;padding:.1em .3em;border-radius:3px}}</style>
<h1>Sign-in required</h1>
<p>{text}</p>
<p>{hint} <code>{path}</code></p>"""


def _login_response(request: web.Request) -> web.Response:
    cfg: DaemonConfig = request.app["cfg"]
    page = _LOGIN_PAGE.format(
        text=_html.escape(tr('This Web UI needs a token so that other users of this computer cannot use it. Open the sign-in link that the daemon printed in its log (it ends with ?token=...).')),
        hint=_html.escape(tr('The token is stored in the file')), path=_html.escape(str(cfg.web_token_path)))
    return web.Response(text=page, status=401, content_type="text/html")


@web.middleware
async def auth(request: web.Request, handler):
    """Every route, /ws included, needs the Web token (cookie or Bearer); `/?token=...` signs in and sets the cookie."""
    token = request.app.get("web_token")
    if token is None:
        return await handler(request)
    candidate = request.query.get("token")
    if request.method == "GET" and request.path == "/" and candidate is not None:
        if not token_matches(candidate, token):
            return _login_response(request)
        resp = web.Response(status=302, headers={"Location": "/"})            # drops the token from the address bar
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", path="/", max_age=365 * 24 * 3600)
        return resp
    if authorized(token, request.headers.get("Authorization"), request.cookies.get(SESSION_COOKIE)):
        return await handler(request)
    if request.method == "GET" and request.path == "/":
        return _login_response(request)
    return web.json_response({"error": tr('authentication required'), "code": "auth"}, status=401)


@web.middleware
async def errors(request: web.Request, handler):
    """Daemon ControlError -> HTTP status + JSON {"error": "..."}."""
    try:
        return await handler(request)
    except asyncio.CancelledError:
        # aiohttp cancels in-flight handlers on shutdown; re-raising trips InvalidStateError
        # in its own bookkeeping, so answer 503 instead.
        if request.app.get("stopping"):
            return web.json_response({"error": "server is shutting down"}, status=503)
        raise
    except ControlError as e:
        status = _HTTP_BY_CODE.get(e.code, 500)
        return web.json_response({"error": str(e), "code": e.code}, status=status)


async def _body(request: web.Request) -> Any:
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": tr('the body must be JSON')}),
                                 content_type="application/json") from None


_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


def _req_lang(request: web.Request) -> Optional[str]:
    """The language of the page that asks: header X-Lang (fetch) or ?lang= (WebSocket); otherwise the daemon-wide one."""
    code = request.headers.get("X-Lang") or request.query.get("lang") or ""
    return code if _LANG_RE.match(code) else None


def localize(obj: Any, lang: Optional[str]) -> Any:
    """Renders every language-neutral "protocol" description inside ``obj`` into plain strings in ``lang``."""
    if isinstance(obj, list):
        return [localize(x, lang) for x in obj]
    if isinstance(obj, dict):
        return {k: (render_protocol(v, lang) if k == "protocol" and isinstance(v, dict) and "items" in v
                    else localize(v, lang)) for k, v in obj.items()}
    return obj


def _rpc(request: web.Request):
    call = request.app["client"].call
    lang = _req_lang(request)

    async def rpc(*args, **kwargs):
        if lang:
            kwargs.setdefault("lang", lang)            # the daemon builds its texts in the language of this page
        return localize(await call(*args, **kwargs), lang)
    return rpc


# ===========================================================================
# WEBSOCKET HUB
# ===========================================================================
class WsHub:
    """Broadcasts the daemon's events to all browsers. Each client has its own bounded
    queue and its own sender task: a slow browser does not slow the others, and its stale `state` frames
    are simply dropped (the UI needs only the latest one).
    """

    def __init__(self) -> None:
        self.clients: Dict[web.WebSocketResponse, asyncio.Queue] = {}
        self.langs: Dict[web.WebSocketResponse, Optional[str]] = {}

    def broadcast(self, event: dict) -> None:
        if not self.clients:
            return
        texts: Dict[Optional[str], str] = {}               # one rendering per language
        droppable = event.get("event") == "state"
        for ws, q in self.clients.items():
            lang = self.langs.get(ws)
            if lang not in texts:
                texts[lang] = json.dumps(localize(event, lang), separators=(",", ":"), ensure_ascii=False)
            text = texts[lang]
            if q.full():
                if droppable:
                    continue
                try:
                    q.get_nowait()                 
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(text)

    async def serve(self, ws: web.WebSocketResponse, hello: list, lang: Optional[str] = None) -> None:
        q: asyncio.Queue = asyncio.Queue(WS_QUEUE)
        self.clients[ws] = q
        self.langs[ws] = lang
        for ev in hello:
            q.put_nowait(json.dumps(localize(ev, lang), ensure_ascii=False))

        async def pump() -> None:
            try:
                while True:
                    await ws.send_str(await q.get())
            except (ConnectionResetError, RuntimeError):
                pass                               

        sender = asyncio.create_task(pump())
        try:
            async for msg in ws:                   
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            sender.cancel()
            self.clients.pop(ws, None)
            self.langs.pop(ws, None)


# ===========================================================================

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


@routes.post("/api/devices/{dev}/mode")
async def dev_mode(request):
    body = await _body(request)
    return web.json_response(await _rpc(request)("set_mode", device=request.match_info["dev"],
                                                 layout=(body or {}).get("layout")))


@routes.post("/api/devices/{dev}/options")
async def dev_options(request):
    body = await _body(request) or {}
    return web.json_response(await _rpc(request)("set_options", device=request.match_info["dev"],
                                                 output_kind=body.get("output_kind"), hide=body.get("hide")))


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
    """Hot settings change: the body is a JSON merge-patch (RFC 7386); ?save=1 also writes it to disk."""
    patch = await _body(request)
    save = request.query.get("save") in ("1", "true")
    return web.json_response(await _rpc(request)("patch_profile", device=request.match_info["dev"],
                                                 patch=patch, save=save))


@routes.post("/api/devices/{dev}/gyro/calibrate")
async def dev_gyro_cal(request):
    body = (await _body(request)) if request.can_read_body else {}
    return web.json_response(await _rpc(request)("calibrate_gyro", device=request.match_info["dev"],
                                                 save=bool((body or {}).get("save", True))))


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

# ---- service operations (previously CLI-only) ----
@routes.get("/api/system")
async def system_info(request):
    return web.json_response(await _rpc(request)("system_info"))


@routes.post("/api/system/sdl-list")
async def system_sdl(request):
    return web.json_response(await _rpc(request)("sdl_list"))


@routes.post("/api/system/unhide")
async def system_unhide(request):
    return web.json_response(await _rpc(request)("unhide"))


@routes.post("/api/system/udev-rules")
async def system_udev(request):
    b = (await _body(request)) if request.can_read_body else {}
    return web.json_response(await _rpc(request)("udev_rules", vid_pid=b.get("vid_pid") or [],
                                                 hide=b.get("hide") or [], group=b.get("group")))


@routes.get("/api/system/keyboards")
async def system_keyboards(request):
    return web.json_response(await _rpc(request)("keyboards"))


@routes.post("/api/system/hidhide")
async def system_hidhide(request):
    b = await _body(request)
    return web.json_response(await _rpc(request)("hidhide", vid=b.get("vid"), pid=b.get("pid")))


@routes.post("/api/profiles-check")
async def profile_check(request):
    b = await _body(request)
    return web.json_response(await _rpc(request)("check_profile", data=b.get("data"), layout=b.get("layout")))


@routes.post("/api/devices/{dev}/autolayout")
async def dev_autolayout(request):
    b = (await _body(request)) if request.can_read_body else {}
    return web.json_response(await _rpc(request)("autolayout", device=request.match_info["dev"],
                                                 save=bool((b or {}).get("save", False))))


@routes.post("/api/devices/{dev}/rumble")
async def dev_rumble(request):
    b = (await _body(request)) if request.can_read_body else {}
    b = b or {}
    return web.json_response(await _rpc(request)("rumble", device=request.match_info["dev"],
                                                 strong=b.get("strong", 0.6), weak=b.get("weak", 0.6),
                                                 ms=b.get("ms", 400)))


@routes.get("/api/locales")
async def locales_list(request: web.Request):
    cfg: DaemonConfig = request.app["cfg"]
    return web.json_response({"configured": cfg.language, "available": list_locales(cfg.locales_dir)})


@routes.get("/api/locale/{code}")
async def locale_get(request: web.Request):
    cfg: DaemonConfig = request.app["cfg"]
    loc = load_locale(request.match_info["code"], cfg.locales_dir)
    if loc is None:
        raise web.HTTPNotFound(text=json.dumps({"error": "unknown locale"}), content_type="application/json")
    return web.json_response(loc)


@routes.get("/ws")
async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
    await ws.prepare(request)
    client: ControlClient = request.app["client"]
    hello = [{"event": "daemon", "connected": client.connected.is_set()}]
    if client.connected.is_set():
        try:
            hello.append({"event": "devices", "devices": await client.call("devices", lang=_req_lang(request))})
        except ControlError:
            pass
    await request.app["hub"].serve(ws, hello, _req_lang(request))
    return ws


# ===========================================================================

# ===========================================================================
def create_app(cfg: DaemonConfig) -> web.Application:
    app = web.Application(middlewares=[guard, auth, errors], client_max_size=1 << 20)
    app["cfg"] = cfg
    hub = WsHub()
    app["hub"] = hub
    app["web_token"] = read_or_create_token(cfg.web_token_path) if cfg.web_auth else None
    token = read_or_create_token(cfg.token_path) if cfg.endpoint().startswith("tcp:") else None

    def on_connection(up: bool) -> None:
        hub.broadcast({"event": "daemon", "connected": up})

    app["client"] = ControlClient(cfg.endpoint(), token, on_event=hub.broadcast,
                                  on_connection=on_connection,
                                  subscribe=["state", "devices", "profile", "stats"])

    async def startup(app: web.Application) -> None:
        app["client"].start()

    async def mark_stopping(app: web.Application) -> None:
        app["stopping"] = True

    async def cleanup(app: web.Application) -> None:
        await app["client"].close()

    app.on_startup.append(startup)
    app.on_shutdown.append(mark_stopping)
    app.on_cleanup.append(cleanup)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app


def run_web(cfg: DaemonConfig) -> None:
    """Entry point of the Web process (called from the CLI or multiprocessing.Process)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s web %(levelname)s %(message)s")
    app = create_app(cfg)
    if app["web_token"]:
        log.info("Web UI: http://%s:%d/?token=%s   (sign-in link; the token is in %s)",
                 cfg.web_host, cfg.web_port, app["web_token"], cfg.web_token_path)
    else:
        log.info("Web UI: http://%s:%d/", cfg.web_host, cfg.web_port)
    web.run_app(app, host=cfg.web_host, port=cfg.web_port,
                access_log=None, print=None, handle_signals=True)
