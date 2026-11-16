"""Device manager: hotplug polling, opening sessions, events for the UI.

Runs in the daemon's asyncio thread (cold path). Blocking operations (enumerate,
open + handshake, which contains a sleep) are moved to an executor, so the event
loop — and with it the control server and rumble — never stalls.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..config import DaemonConfig
from ..input import BackendUnavailable, DeviceInfo, drop_shadowed_evdev, enumerate_devices as _default_enumerate, open_device as _default_open
from ..logic.autolayout import layout_key, try_layout
from ..logic.gyrocal import GyroCalError, estimate_bias
from ..logic.keyboard import KeyMap, KeyMapError, build_layout as build_keyboard_layout, load_keymap
from ..logic.layout import DeviceLayout
from ..logic.profile import ProfileError
from ..logic.outputs import output_choices, pick_kind
from ..logic.protocol import describe_protocol
from ..output import create_output as _default_create_output
from ..store import ProfileStore, StoreError
from .session import Session, device_id, device_key
from ..i18n import tr
from ..util import NotFoundError

log = logging.getLogger(__name__)

RETRY_FAILED_S = 5.0     


class ManagerError(Exception):
    pass


class ManagerNotFound(ManagerError, NotFoundError):
    pass


class Manager:
    def __init__(self, cfg: DaemonConfig, store: ProfileStore, layouts: Dict[str, DeviceLayout], *,
                 enumerate_fn: Callable[[str], List[DeviceInfo]] = _default_enumerate,
                 open_fn=_default_open, output_fn=_default_create_output,
                 keyboard_enumerate_fn: Optional[Callable[[List[str]], List[DeviceInfo]]] = None,
                 keyboard_open_fn: Optional[Callable] = None):
        self.cfg = cfg
        self.store = store
        self.layouts = layouts
        self._enumerate = enumerate_fn
        self._open_fn = open_fn
        self._output_fn = output_fn
        self._kb_enumerate = keyboard_enumerate_fn
        self._kb_open = keyboard_open_fn
        self._keymap: Optional[KeyMap] = None
        self._keymap_error: Optional[str] = None
        self.sessions: Dict[str, Session] = {}
        self._auto: Dict[tuple, Optional[DeviceLayout]] = {}   
        self.known: Dict[str, DeviceInfo] = {}          
        self.failed: Dict[str, tuple] = {}              # id -> (time, message)
        self._backend_msg = ""                          
        self._fail_count: Dict[str, int] = {}           # id -> number of consecutive failed attempts (for backoff)
        self._opening: set = set()
        self._declined: set = set()                     # ids the user closed manually
        self.subscribers: Dict[Callable[[dict], None], set] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: List[asyncio.Task] = []
        self._stopping = False

    # ===================================================================
    
    # ===================================================================
    def subscribe(self, cb: Callable[[dict], None], events: set) -> None:
        self.subscribers[cb] = set(events)

    def unsubscribe(self, cb: Callable[[dict], None]) -> None:
        self.subscribers.pop(cb, None)

    def emit(self, event: dict) -> None:
        name = event["event"]
        for cb, wanted in list(self.subscribers.items()):
            if name in wanted:
                try:
                    cb(event)
                except Exception:  # noqa: BLE001
                    log.debug(tr('subscriber failure'), exc_info=True)

    def _wants(self, name: str) -> bool:
        return any(name in w for w in self.subscribers.values())

    # ===================================================================
    
    # ===================================================================
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._restore_hidden()
        await self.scan()
        self._tasks = [asyncio.create_task(self._scan_loop(), name="scan"),
                       asyncio.create_task(self._tick_loop(), name="tick")]

    def _restore_hidden(self) -> None:
        """After a crash of the previous run, nodes may have stayed hidden: restore their permissions."""
        if self._enumerate is not _default_enumerate:       
            return
        try:
            from ..config import default_runtime_dir
            from ..input.hide_linux import restore_stale
            done, bad = restore_stale(default_runtime_dir() / "hidden.json")
        except Exception:  # noqa: BLE001
            return
        if done:
            log.info(tr('permissions restored on %d node(s) after the previous run'), len(done))
        if bad:
            log.warning(tr('could not restore permissions: %s (run `gamepad-remapd unhide` or `sudo udevadm trigger`)'), bad[0])

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for s in list(self.sessions.values()):
            s.stop()          
        self.sessions.clear()

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.poll_interval_s)
            try:
                await self.scan()
            except Exception:  # noqa: BLE001
                log.exception(tr('hotplug polling error'))

    async def _tick_loop(self) -> None:
        """Live state for the Web UI: ~ui_hz frames/s, only changed items; stats once per second."""
        period = 1.0 / max(1, self.cfg.ui_hz)
        next_stats = time.monotonic() + 1.0
        while True:
            await asyncio.sleep(period)
            if self._wants("state"):
                for s in list(self.sessions.values()):
                    snap = s.snapshot()
                    if snap != s._last_snap:
                        s._last_snap = snap
                        self.emit({"event": "state", **snap})
            now = time.monotonic()
            if now >= next_stats:
                next_stats = now + 1.0
                if self._wants("stats"):
                    self.emit({"event": "stats",
                               "stats": {sid: s.stats() for sid, s in self.sessions.items()}})

    # ===================================================================
    
    # ===================================================================
    # ---- keyboard as gamepad ----
    def keyboard_keymap(self) -> Optional[KeyMap]:
        """Keymap from the configuration (cached; a file error is kept in `_keymap_error`, the daemon does not crash)."""
        if self._keymap is None and self._keymap_error is None:
            try:
                self._keymap = load_keymap(self.cfg.keyboard.keymap, self.cfg.keymap_dirs())
            except KeyMapError as e:
                self._keymap_error = str(e)
                log.error("keyboard: %s", e)
        return self._keymap

    def _keyboard_infos(self) -> List[DeviceInfo]:
        kb = self.cfg.keyboard
        if not kb.enabled or self.cfg.backend in ("hidapi", "winhid"):
            return []
        if self._kb_enumerate is not None:
            return self._kb_enumerate(list(kb.device))
        try:
            from ..input.keyboard_linux import enumerate_keyboards
        except ImportError:
            return []
        return enumerate_keyboards(list(kb.device))

    def _open_dispatch(self, info: DeviceInfo, read_size: int):
        """Device factory for Session: keyboards are opened here (they need the keymap and settings)."""
        if info.extra and info.extra.get("kind") == "keyboard":
            km = self.keyboard_keymap()
            if km is None:
                raise ManagerError(tr('keymap unavailable: {0}', self._keymap_error))
            if self._kb_open is not None:
                return self._kb_open(info, km, read_size, self.cfg.keyboard)
            from ..input.keyboard_linux import KeyboardDevice
            dev = KeyboardDevice(info, km, read_size, grab=self.cfg.keyboard.grab, toggle=self.cfg.keyboard.toggle)
            dev.open()
            return dev
        return self._open_fn(info, read_size)

    def pick_layout(self, info: DeviceInfo) -> Optional[DeviceLayout]:
        """Explicit overrides from daemon.json take priority, then a match on VID/PID(/interface);
        more specific entries (with an interface set) come before general ones.
        """
        if info.extra and info.extra.get("kind") == "keyboard":
            km = self.keyboard_keymap()
            return build_keyboard_layout(km, info.vid, info.pid) if km else None
        if info.backend == "evdev":
            return self._auto_layout(info)      # evdev devices have their own (synthetic) report format
        for o in self.cfg.overrides:
            try:
                if (int(str(o["vid"]), 16), int(str(o["pid"]), 16)) == (info.vid, info.pid) and \
                        o.get("layout") in self.layouts:
                    if o.get("interface") is None or o["interface"] == info.interface:
                        return self.layouts[o["layout"]]
            except (KeyError, ValueError):
                continue
        cands = [l for l in self.layouts.values() if l.matches(info.vid, info.pid, info.interface, info.bus)]
        cands.sort(key=lambda l: (l.interface is None, -l.priority, not (l.mode and l.mode.default)))
        if cands:
            want = self.store.get_mode(device_key(info))          
            if want:
                for l in cands:
                    if l.id == want and l.mode:
                        return l
            return cands[0]
        return self._auto_layout(info)          

    def modes_for(self, info: DeviceInfo, current: Optional[DeviceLayout]) -> List[Dict[str, Any]]:
        """Protocol modes the device supports (layouts with a `mode` in the same group); fewer than 2 means there is no choice."""
        if current is None or current.mode is None:
            return []
        group = [l for l in self.layouts.values()
                 if l.mode and l.mode.group == current.mode.group
                 and l.matches(info.vid, info.pid, info.interface, info.bus)]
        group.sort(key=lambda l: (not l.mode.default, l.id))
        if len(group) < 2:
            return []
        return [{"id": l.id, "mode": l.mode.id, "label": l.mode.label, "note": l.mode.note,
                 "current": l.id == current.id, "gyro": bool(l.gyro)} for l in group]

    def device_options(self, info: DeviceInfo, layout: Optional[DeviceLayout]) -> Dict[str, Any]:
        """Output protocol + original-device hiding for the model (user choice or the default)."""
        saved = self.store.get_opts(device_key(info))
        choices = output_choices(info.vid, layout.id if layout else None)
        hide = saved.get("hide")
        return {"output_kind": pick_kind(saved.get("output_kind"), choices, self.cfg.output_kind),
                "output_choices": choices,
                "hide": bool(self.cfg.grab_siblings if hide is None else hide)}

    def _auto_layout(self, info: DeviceInfo) -> Optional[DeviceLayout]:
        """Builds a layout from the descriptor of an unknown device (with a cache).

                pick_layout() is called every second (hotplug polling) and on every list_devices(),
                so descriptor parsing runs once per (VID, PID, interface, descriptor); a "negative"
                result (not a gamepad, e.g. the keyboard interface of a receiver) is cached too.
        """
        if not self.cfg.auto_layout or not (info.descriptor or info.extra):
            return None
        
        
        
        if info.backend != "evdev" and any(l.vid == info.vid and info.pid in l.pids
                                           for l in self.layouts.values()):
            return None
        key = (info.vid, info.pid, info.interface, info.backend, layout_key(info))
        if key not in self._auto:
            lay = try_layout(info)
            self._auto[key] = lay
            if lay is not None:
                log.info(tr('%04x:%04x iface=%s: no ready layout — composed from the HID descriptor (%s). Button/axis roles are heuristics, check: gamepad-remapd autolayout --save'),
                         info.vid, info.pid, info.interface, lay.id)
        return self._auto[key]

    def all_layouts(self) -> Dict[str, DeviceLayout]:
        """Layouts from disk + automatically built ones (for IPC/UI and open_device)."""
        out = dict(self.layouts)
        for lay in self._auto.values():
            if lay is not None:
                out.setdefault(lay.id, lay)
        return out

    def visible_devices(self) -> List[DeviceInfo]:
        """Device list without evdev "duplicates" of devices that are already read via hidraw with a layout."""
        try:
            found = self._enumerate(self.cfg.backend)
        except BackendUnavailable as e:             
            if self._backend_msg != str(e):
                self._backend_msg = str(e)
                log.error("%s", e)
            found = []
        pads = drop_shadowed_evdev(found, lambda i: self.pick_layout(i) is not None)
        return pads + self._keyboard_infos()

    async def scan(self) -> None:
        infos = await self.loop.run_in_executor(None, self.visible_devices)
        current = {device_id(i): i for i in infos}
        changed = set(current) != set(self.known)
        self.known = current
        self._declined &= set(current)            
        for sid in [k for k in self.failed if k not in current]:
            del self.failed[sid]
            self._fail_count.pop(sid, None)
        if self.cfg.auto_open:
            for sid, info in current.items():
                if sid in self.sessions or sid in self._opening or sid in self._declined:
                    continue
                lay = self.pick_layout(info)
                if lay is None:
                    continue
                f = self.failed.get(sid)
                n_fail = self._fail_count.get(sid, 1)
                
                if f and time.monotonic() - f[0] < min(RETRY_FAILED_S * 2 ** (n_fail - 1), 60.0):
                    continue
                try:
                    await self._open(info, lay)
                except ManagerError:
                    
                    
                    
                    continue
        if changed:
            self.emit({"event": "devices", "devices": self.list_devices()})

    async def _open(self, info: DeviceInfo, layout: Optional[DeviceLayout]) -> Session:
        sid = device_id(info)
        if sid in self.sessions:
            raise ManagerError(tr('{0}: already open', sid))
        self._opening.add(sid)
        opts = self.device_options(info, layout)
        s = Session(info, layout, self.cfg, self.store, open_fn=self._open_dispatch,
                    output_fn=self._output_fn, on_dead=self._thread_dead,
                    opts={"output_kind": opts["output_kind"], "hide": opts["hide"]})
        try:
            await self.loop.run_in_executor(None, s.prepare)   # open + handshake + uinput
            s.activate(self.loop)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            first = self.failed.get(sid, (0, None))[1] != msg        
            self.failed[sid] = (time.monotonic(), msg)
            self._fail_count[sid] = self._fail_count.get(sid, 0) + 1
            if first:
                level = logging.WARNING if isinstance(e, PermissionError) else logging.ERROR
                hint = (self._group_hint(info) if isinstance(e, PermissionError) and self._group_hint(info) else
                        tr('\n'
                           '  udev rules are needed for {0:04x}:{1:04x}: gamepad-remapd udev-rules --vid-pid {2:04x}:{3:04x} | sudo tee /etc/udev/rules.d/70-gamepad-remapd.rules; sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw (then reconnect a Bluetooth device)', info.vid, info.pid, info.vid, info.pid)
                        if isinstance(e, PermissionError) else "")
                log.log(level, tr('%s: could not open: %s%s'), sid, e, hint)
            raise ManagerError(f"{sid}: {e}") from e
        finally:
            self._opening.discard(sid)
        self.failed.pop(sid, None)
        self._fail_count.pop(sid, None)
        self.sessions[sid] = s
        log.info(tr('%s: session started (%s, layout=%s, profile=%s)'),
                 sid, info.name or "?", layout.id if layout else "—", s.profile_name)
        self.emit({"event": "devices", "devices": self.list_devices()})
        return s

    @staticmethod
    def _group_hint(info: DeviceInfo) -> str:
        """hidraw is closed by the hiding group (setup-hide), but the daemon was started without it."""
        try:
            import os as _os
            from ..input.hidraw_linux import HIDE_GROUP
            from ..tools.setup_hide import group_gid
            gid = group_gid(HIDE_GROUP)
            if gid is not None and _os.stat(info.path).st_gid == gid:
                return (tr('\n'
                           '  {0} is closed to group {1} (setup-hide): run the daemon with it — `sudo -g {2} -u $USER env PYTHONPATH=src python3 -m gamepad_remapd serve` or the service (`setup-hide --service`); to undo the hiding: `setup-hide --uninstall`', info.path, HIDE_GROUP, HIDE_GROUP))
        except (OSError, ImportError):
            pass
        return ""

    def _thread_dead(self, s: Session) -> None:
        """Called from the reader thread — we switch to the asyncio thread."""
        loop = self.loop
        if loop is not None and not self._stopping and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._reap(s)))

    async def _reap(self, s: Session) -> None:
        if self.sessions.get(s.id) is s:
            del self.sessions[s.id]
            s.stop()          
            self.failed[s.id] = (time.monotonic(), s.error or tr('finished'))
            self.emit({"event": "devices", "devices": self.list_devices()})

    # ===================================================================
    # API FOR THE CONTROL SERVER
    # ===================================================================
    def get(self, sid: Optional[str]) -> Session:
        if sid is None:
            if len(self.sessions) == 1:
                return next(iter(self.sessions.values()))
            raise ManagerError(tr('specify device (open sessions: %d)') % len(self.sessions))
        try:
            return self.sessions[sid]
        except KeyError:
            raise ManagerNotFound(tr('device {0!r} is not open', sid)) from None

    def list_devices(self) -> List[Dict[str, Any]]:
        out = []
        for sid, info in self.known.items():
            s = self.sessions.get(sid)
            if s is not None:
                d = s.describe()
                d["modes"] = self.modes_for(info, s.layout)
                d["options"] = self.device_options(info, s.layout)
                out.append(d)
                continue
            lay = self.pick_layout(info)
            d = {**info.as_dict(), "id": sid, "open": False,
                 "layout": lay.id if lay else None, "layout_name": lay.name if lay else None,
                 "error": (self.failed.get(sid) or (0, None))[1],
                 "protocol": describe_protocol(info, lay), "modes": self.modes_for(info, lay),
                 "options": self.device_options(info, lay)}
            out.append(d)
        return out

    async def open_device(self, sid: str, layout_id: Optional[str]) -> Dict[str, Any]:
        info = self.known.get(sid)
        if info is None:
            raise ManagerNotFound(tr('device {0!r} not found', sid))
        lay = None
        if layout_id:
            lay = self.all_layouts().get(layout_id)
            if lay is None:
                raise ManagerNotFound(tr('layout {0!r} not found', layout_id))
        self._declined.discard(sid)
        if sid in self.sessions:                  
            await self.close_device(sid, decline=False)
        s = await self._open(info, lay)
        return s.describe()

    async def set_mode(self, sid: str, layout_id: str) -> Dict[str, Any]:
        """Select the protocol mode: remember it for the model and reopen the device (with its handshake)."""
        info = self.known.get(sid)
        if info is None:
            raise ManagerNotFound(tr('device {0!r} not found', sid))
        cur = self.sessions[sid].layout if sid in self.sessions else self.pick_layout(info)
        ids = {m["id"] for m in self.modes_for(info, cur)}
        if layout_id not in ids:
            raise ManagerError(tr('mode {0!r} is unavailable for {1} (available: {2})', layout_id, sid, ', '.join(sorted(ids)) or '—'))
        self.store.set_mode(device_key(info), layout_id)
        was_open = sid in self.sessions
        if was_open:
            return await self.open_device(sid, layout_id)
        self.emit({"event": "devices", "devices": self.list_devices()})
        return {"id": sid, "layout": layout_id, "open": False}

    async def set_options(self, sid: str, output_kind: Optional[str] = None,
                          hide: Optional[bool] = None) -> Dict[str, Any]:
        """Output protocol (XInput emulation / native) and original-device hiding; the open device is reopened."""
        info = self.known.get(sid)
        if info is None:
            raise ManagerNotFound(tr('device {0!r} not found', sid))
        s = self.sessions.get(sid)
        lay = s.layout if s is not None else self.pick_layout(info)
        if output_kind is not None:
            ids = {c["id"] for c in output_choices(info.vid, lay.id if lay else None)}
            if output_kind not in ids:
                raise ManagerError(tr('output {0!r} is unavailable for {1} (available: {2})', output_kind, sid, ', '.join(sorted(ids))))
        if hide is not None and not isinstance(hide, bool):
            raise ManagerError(tr('hide must be true/false'))
        self.store.set_opts(device_key(info), output_kind=output_kind, hide=hide)
        if s is not None:
            return await self.open_device(sid, lay.id if lay else None)
        self.emit({"event": "devices", "devices": self.list_devices()})
        return {"id": sid, "open": False, **self.device_options(info, lay)}

    async def close_device(self, sid: str, decline: bool = True) -> None:
        s = self.get(sid)
        del self.sessions[sid]
        if decline:
            self._declined.add(sid)               
        s.stop()              # wake() wakes poll(); join() takes milliseconds
        self.emit({"event": "devices", "devices": self.list_devices()})

    def activate_profile(self, sid: Optional[str], name: str) -> Dict[str, Any]:
        s = self.get(sid)
        cp = s.set_profile(self.store.get(name))
        self.store.set_active(device_key(s.info), cp.name)
        self.emit({"event": "profile", "dev": s.id, "name": cp.name, "data": cp.data,
                   "warnings": cp.warnings})
        return {"name": cp.name, "warnings": cp.warnings, "data": cp.data}

    def patch_profile(self, sid: Optional[str], patch: dict, save: bool = False) -> Dict[str, Any]:
        s = self.get(sid)
        cp = s.patch_profile(patch)
        if save:
            self.store.save(cp.name, cp.data)
            self.store.set_active(device_key(s.info), cp.name)
        self.emit({"event": "profile", "dev": s.id, "name": cp.name, "data": cp.data,
                   "warnings": cp.warnings})
        return {"name": cp.name, "warnings": cp.warnings, "data": cp.data, "saved": save}

    async def calibrate_gyro(self, sid: Optional[str], duration: float = 1.5, save: bool = False) -> Dict[str, Any]:
        """Gyro zero: collect raw reports for ~1.5 s and write the average to `gyro.bias` of the active profile."""
        s = self.get(sid)
        lg = s.layout.gyro if s.layout else None
        if not lg or s.parser is None:
            raise ManagerError(tr("this device's layout does not describe a gyroscope"))
        idx = s.parser.index
        iy, ip = idx[lg["yaw"]], idx[lg["pitch"]]
        loop = asyncio.get_running_loop()
        end = loop.time() + max(0.5, min(float(duration), 5.0))
        ys: List[float] = []
        ps: List[float] = []
        last = None
        while loop.time() < end:
            raw = s.pipeline.last_raw
            if raw and raw is not last and len(raw) >= s.parser.min_len:
                last = raw
                _, vals = s.parser.parse(raw)
                ys.append(vals[iy])
                ps.append(vals[ip])
            await asyncio.sleep(0.002)
        try:
            r = estimate_bias(ys, ps, float(lg["dps_per_lsb"]))
        except GyroCalError as e:
            raise ManagerError(str(e)) from e
        res = self.patch_profile(sid, {"gyro": {"bias": r["bias"]}}, save)
        return {**r, "saved": save, "data": res["data"]}


__all__ = ["Manager", "ManagerError", "ProfileError", "StoreError"]
