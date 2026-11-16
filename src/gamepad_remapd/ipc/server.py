"""Control server of the daemon: accepts JSON commands from the Web process (and the CLI).

All commands run in the daemon's asyncio thread. None of them touches the hot path,
except the atomic profile swap (`Session.set_profile`).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from ..core import sysops
from ..core.manager import Manager, ManagerError, ManagerNotFound
from ..util import NotFoundError
from ..logic.curves import CurveError
from ..logic.layout import LayoutError
from ..logic.profile import ProfileError
from ..store import ProfileStore, StoreError
from . import protocol
from ..i18n import reset_language, tr, use_language

log = logging.getLogger(__name__)



MAX_WRITE_BUFFER = 256 * 1024

_ERR_CODES = ((NotFoundError, "not_found"), (ProfileError, "profile"), (CurveError, "profile"), (StoreError, "store"),
              (LayoutError, "layout"), (ManagerError, "device"), (KeyError, "param"),
              (ValueError, "param"))


class ControlServer:
    def __init__(self, manager: Manager, store: ProfileStore, endpoint: str,
                 token: Optional[str] = None):
        self.m = manager
        self.store = store
        self.endpoint = endpoint
        self.token = token                         # None => no authentication needed (unix socket)
        self._server: Optional[asyncio.AbstractServer] = None
        self._handlers: Dict[str, Callable[[dict], Awaitable[Any]]] = {
            "ping": self._ping, "devices": self._devices, "rescan": self._rescan,
            "layouts": self._layouts, "layout": self._layout,
            "open": self._open, "close": self._close, "set_mode": self._set_mode, "set_options": self._set_options,
            "profiles": self._profiles, "profile_get": self._profile_get,
            "profile_save": self._profile_save, "profile_delete": self._profile_delete,
            "active_profile": self._active_profile, "activate": self._activate,
            "patch_profile": self._patch_profile, "calibrate_gyro": self._calibrate_gyro,
            "stats": self._stats, "stats_reset": self._stats_reset,
            "sniff": self._sniff,
            "system_info": self._system_info, "sdl_list": self._sdl_list, "unhide": self._unhide,
            "udev_rules": self._udev_rules, "keyboards": self._keyboards, "hidhide": self._hidhide,
            "check_profile": self._check_profile, "autolayout": self._autolayout, "rumble": self._rumble,
        }

    async def start(self) -> None:
        self._server = await protocol.start_server(self._client, self.endpoint)
        log.info(tr('control server listening on %s'), self.endpoint)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ===================================================================
    
    # ===================================================================
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        authed = self.token is None

        def send(obj: dict) -> None:
            if not writer.is_closing():
                writer.write(protocol.dumps(obj))

        def on_event(ev: dict) -> None:
            tr = writer.transport
            if ev["event"] == "state" and tr.get_write_buffer_size() > MAX_WRITE_BUFFER:
                return                              
            send(ev)

        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    send({"ok": False, "error": tr('message too large'), "code": "param"})
                    break
                if not line:
                    break
                req: Any = None
                try:
                    req = protocol.loads(line)
                    if not isinstance(req, dict) or "cmd" not in req:
                        raise ValueError(tr("an object with a 'cmd' field is expected"))
                    cmd = req["cmd"]
                    if not authed:
                        
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
                        raise ValueError(tr('unknown command {0!r}', cmd))
                    lang_token = use_language(req.get("lang"))      # the language of the asking page, if it sent one
                    try:
                        result = await h(req)
                    finally:
                        reset_language(lang_token)
                    send({"id": req.get("id"), "ok": True, "result": result})
                except Exception as e:  # noqa: BLE001
                    code = next((c for t, c in _ERR_CODES if isinstance(e, t)), "internal")
                    if code == "internal":
                        log.exception(tr('command error'))
                    msg = tr('missing parameter {0}', e) if isinstance(e, KeyError) else str(e)
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
                 "rumble": l.rumble is not None, "mode": (l.mode.group + "/" + l.mode.id) if l.mode else None,
                 "handshake_steps": len(l.handshake),
                "generated": l.generated}
                for l in self.m.all_layouts().values()]

    async def _layout(self, p: dict) -> dict:
        l = self.m.all_layouts().get(p["id"])
        if l is None:
            raise ManagerNotFound(tr('layout {0!r} not found', p['id']))
        return {"id": l.id, "name": l.name, "generated": l.generated, "notes": l.notes,
                "buttons": {n: {"byte": b.byte, "bit": b.bit, "target": b.target}
                            for n, b in l.buttons.items()},
                "fields": sorted(l.fields), "sticks": l.sticks, "triggers": l.triggers}

    async def _open(self, p: dict) -> dict:
        return await self.m.open_device(p["device"], p.get("layout"))

    async def _set_options(self, p: dict) -> dict:
        return await self.m.set_options(p["device"], p.get("output_kind"), p.get("hide"))

    async def _set_mode(self, p: dict) -> dict:
        return await self.m.set_mode(p["device"], p["layout"])

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
            raise ValueError(tr('patch must be a JSON object (merge-patch, RFC 7386)'))
        return self.m.patch_profile(p.get("device"), patch, bool(p.get("save", False)))

    async def _calibrate_gyro(self, p: dict) -> dict:
        return await self.m.calibrate_gyro(p.get("device"), float(p.get("duration", 1.5)), bool(p.get("save", False)))

    async def _stats(self, p: dict) -> dict:
        return self.m.get(p.get("device")).stats()

    async def _stats_reset(self, p: dict) -> dict:
        self.m.get(p.get("device")).pipeline.reset_stats()
        return {}

    async def _sniff(self, p: dict) -> dict:
        return self.m.get(p.get("device")).sniff(p.get("action", "summary"), p.get("name"),
                                                 p.get("baseline_s"))

    # ---- service operations (previously CLI-only) ----
    async def _system_info(self, p: dict) -> dict:
        return sysops.system_info(self.m.cfg)

    async def _sdl_list(self, p: dict) -> dict:
        return await asyncio.to_thread(sysops.sdl_list)

    async def _unhide(self, p: dict) -> dict:
        return await asyncio.to_thread(sysops.unhide)

    async def _udev_rules(self, p: dict) -> dict:
        return sysops.udev_rules(self.m.all_layouts(), list(p.get("vid_pid") or []),
                                 list(p.get("hide") or []), p.get("group") or None)

    async def _keyboards(self, p: dict) -> dict:
        return await asyncio.to_thread(sysops.keyboards, self.m.cfg)

    async def _hidhide(self, p: dict) -> dict:
        vid, pid = int(str(p["vid"]), 16), int(str(p["pid"]), 16)
        paths = [i.path for i in self.m.visible_devices() if (i.vid, i.pid) == (vid, pid)]
        return sysops.hidhide_commands(vid, pid, paths)

    async def _check_profile(self, p: dict) -> dict:
        return sysops.check_profile(p["data"], p.get("layout"), self.m.all_layouts())

    def _info_for(self, dev: str):
        from ..core.session import device_id
        s = self.m.sessions.get(dev)
        if s is not None:
            return s.info
        for i in self.m.visible_devices():
            if device_id(i) == dev:
                return i
        raise ManagerNotFound(tr('device {0!r} not found', dev))

    async def _autolayout(self, p: dict) -> dict:
        info = self._info_for(p["device"])
        res = sysops.autolayout(info, bool(p.get("save", False)), self.m.cfg.devices_dir)
        if res.get("saved"):
            await self.m.scan()                       
        return res

    async def _rumble(self, p: dict) -> dict:
        s = self.m.get(p["device"])
        return await asyncio.to_thread(sysops.rumble, s, float(p.get("strong", 0.6)),
                                       float(p.get("weak", 0.6)), int(p.get("ms", 400)))
