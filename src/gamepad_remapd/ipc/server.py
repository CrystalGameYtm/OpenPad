"""Control-сервер демона: приймає JSON-команди від Web-процесу (та CLI).

Усі команди виконуються в asyncio-потоці демона. Жодна з них не торкається
гарячого шляху, окрім атомарної підміни профілю (`Session.set_profile`).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from ..core.manager import Manager, ManagerError
from ..logic.curves import CurveError
from ..logic.layout import LayoutError
from ..logic.profile import ProfileError
from ..store import ProfileStore, StoreError
from . import protocol

log = logging.getLogger(__name__)

# Якщо буфер запису клієнта перевищує це значення, кадри «state» для нього
# пропускаються (повільний браузер не повинен накопичувати пам'ять демона).
MAX_WRITE_BUFFER = 256 * 1024

_ERR_CODES = ((ProfileError, "profile"), (CurveError, "profile"), (StoreError, "store"),
              (LayoutError, "layout"), (ManagerError, "device"), (KeyError, "param"),
              (ValueError, "param"))


class ControlServer:
    def __init__(self, manager: Manager, store: ProfileStore, endpoint: str,
                 token: Optional[str] = None):
        self.m = manager
        self.store = store
        self.endpoint = endpoint
        self.token = token                         # None => автентифікація не потрібна (unix-сокет)
        self._server: Optional[asyncio.AbstractServer] = None
        self._handlers: Dict[str, Callable[[dict], Awaitable[Any]]] = {
            "ping": self._ping, "devices": self._devices, "rescan": self._rescan,
            "layouts": self._layouts, "layout": self._layout,
            "open": self._open, "close": self._close,
            "profiles": self._profiles, "profile_get": self._profile_get,
            "profile_save": self._profile_save, "profile_delete": self._profile_delete,
            "active_profile": self._active_profile, "activate": self._activate,
            "patch_profile": self._patch_profile,
            "stats": self._stats, "stats_reset": self._stats_reset,
            "sniff": self._sniff,
        }

    async def start(self) -> None:
        self._server = await protocol.start_server(self._client, self.endpoint)
        log.info("control-сервер слухає %s", self.endpoint)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ===================================================================
    # З'ЄДНАННЯ
    # ===================================================================
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authed = self.token is None

        def send(obj: dict) -> None:
            if not writer.is_closing():
                writer.write(protocol.dumps(obj))

        def on_event(ev: dict) -> None:
            tr = writer.transport
            if ev["event"] == "state" and tr.get_write_buffer_size() > MAX_WRITE_BUFFER:
                return                              # повільний споживач: пропускаємо кадр
            send(ev)

        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    send({"ok": False, "error": "повідомлення завелике", "code": "param"})
                    break
                if not line:
                    break
                req: Any = None
                try:
                    req = protocol.loads(line)
                    if not isinstance(req, dict) or "cmd" not in req:
                        raise ValueError("очікується об'єкт із полем 'cmd'")
                    cmd = req["cmd"]
                    if not authed:
                        # Перша команда ОБОВ'ЯЗКОВО auth; інакше розриваємо з'єднання.
                        if cmd != "auth" or not hmac.compare_digest(str(req.get("token", "")), self.token or ""):
                            send({"id": req.get("id"), "ok": False, "error": "auth", "code": "auth"})
                            break
                        authed = True
                        send({"id": req.get("id"), "ok": True, "result": {}})
                        continue
                    if cmd == "subscribe":
                        evs = set(req.get("events") or ["state", "devices", "profile", "stats"])
                        self.m.subscribe(on_event, evs)
                        send({"id": req.get("id"), "ok": True, "result": {"events": sorted(evs)}})
                        continue
                    if cmd == "unsubscribe":
                        self.m.unsubscribe(on_event)
                        send({"id": req.get("id"), "ok": True, "result": {}})
                        continue
                    h = self._handlers.get(cmd)
                    if h is None:
                        raise ValueError(f"невідома команда {cmd!r}")
                    result = await h(req)
                    send({"id": req.get("id"), "ok": True, "result": result})
                except Exception as e:  # noqa: BLE001
                    code = next((c for t, c in _ERR_CODES if isinstance(e, t)), "internal")
                    if code == "internal":
                        log.exception("помилка команди")
                    msg = f"відсутній параметр {e}" if isinstance(e, KeyError) else str(e)
                    send({"id": req.get("id") if isinstance(req, dict) else None,
                          "ok": False, "error": msg, "code": code})
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.m.unsubscribe(on_event)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    # ===================================================================
    # КОМАНДИ
    # ===================================================================
    async def _ping(self, p: dict) -> dict:
        return {"pong": True}

    async def _devices(self, p: dict) -> list:
        return self.m.list_devices()

    async def _rescan(self, p: dict) -> list:
        await self.m.scan()
        return self.m.list_devices()

    async def _layouts(self, p: dict) -> list:
        return [{"id": l.id, "name": l.name,
                 "vid": None if l.vid is None else f"{l.vid:04x}",
                 "pid": None if l.pid is None else f"{l.pid:04x}",
                 "buttons": sorted(l.buttons), "gyro": bool(l.gyro),
                 "rumble": l.rumble is not None, "handshake_steps": len(l.handshake),
                "generated": l.generated}
                for l in self.m.all_layouts().values()]

    async def _layout(self, p: dict) -> dict:
        l = self.m.all_layouts().get(p["id"])
        if l is None:
            raise ManagerError(f"розкладку {p['id']!r} не знайдено")
        return {"id": l.id, "name": l.name, "generated": l.generated, "notes": l.notes,
                "buttons": {n: {"byte": b.byte, "bit": b.bit, "target": b.target}
                            for n, b in l.buttons.items()},
                "fields": sorted(l.fields), "sticks": l.sticks, "triggers": l.triggers}

    async def _open(self, p: dict) -> dict:
        return await self.m.open_device(p["device"], p.get("layout"))

    async def _close(self, p: dict) -> dict:
        await self.m.close_device(p["device"])
        return {}

    async def _profiles(self, p: dict) -> list:
        return self.store.list()

    async def _profile_get(self, p: dict) -> dict:
        return self.store.get(p["name"])

    async def _profile_save(self, p: dict) -> dict:
        return self.store.save(p["name"], p["data"])

    async def _profile_delete(self, p: dict) -> dict:
        self.store.delete(p["name"])
        return {}

    async def _active_profile(self, p: dict) -> dict:
        s = self.m.get(p.get("device"))
        return {"name": s.profile_name, "data": s.profile_data, "warnings": s.warnings}

    async def _activate(self, p: dict) -> dict:
        return self.m.activate_profile(p.get("device"), p["name"])

    async def _patch_profile(self, p: dict) -> dict:
        patch = p["patch"]
        if not isinstance(patch, dict):
            raise ValueError("patch має бути JSON-об'єктом (merge-patch, RFC 7386)")
        return self.m.patch_profile(p.get("device"), patch, bool(p.get("save", False)))

    async def _stats(self, p: dict) -> dict:
        return self.m.get(p.get("device")).stats()

    async def _stats_reset(self, p: dict) -> dict:
        self.m.get(p.get("device")).pipeline.reset_stats()
        return {}

    async def _sniff(self, p: dict) -> dict:
        return self.m.get(p.get("device")).sniff(p.get("action", "summary"), p.get("name"),
                                                 p.get("baseline_s"))
