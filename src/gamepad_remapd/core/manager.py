"""Менеджер пристроїв: hotplug-опитування, відкриття сесій, події для UI.

Працює в asyncio-потоці демона (cold path). Блокуючі дії (enumerate, open +
handshake, що містить sleep) виносяться в executor, щоб цикл подій — а отже
control-сервер і rumble — ніколи не «зависали».
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..config import DaemonConfig
from ..input import DeviceInfo, drop_shadowed_evdev, enumerate_devices as _default_enumerate, open_device as _default_open
from ..logic.autolayout import layout_key, try_layout
from ..logic.layout import DeviceLayout
from ..logic.profile import ProfileError
from ..output import create_output as _default_create_output
from ..store import ProfileStore, StoreError
from .session import Session, device_id, device_key

log = logging.getLogger(__name__)

RETRY_FAILED_S = 5.0     # як часто повторювати невдале відкриття (PermissionError тощо)


class ManagerError(Exception):
    pass


class Manager:
    def __init__(self, cfg: DaemonConfig, store: ProfileStore, layouts: Dict[str, DeviceLayout], *,
                 enumerate_fn: Callable[[str], List[DeviceInfo]] = _default_enumerate,
                 open_fn=_default_open, output_fn=_default_create_output):
        self.cfg = cfg
        self.store = store
        self.layouts = layouts
        self._enumerate = enumerate_fn
        self._open_fn = open_fn
        self._output_fn = output_fn
        self.sessions: Dict[str, Session] = {}
        self._auto: Dict[tuple, Optional[DeviceLayout]] = {}   # кеш авторозкладок (див. _auto_layout)
        self.known: Dict[str, DeviceInfo] = {}          # усе, що бачив останній scan
        self.failed: Dict[str, tuple] = {}              # id -> (час, повідомлення)
        self._opening: set = set()
        self._declined: set = set()                     # id, закриті користувачем вручну
        self.subscribers: Dict[Callable[[dict], None], set] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: List[asyncio.Task] = []
        self._stopping = False

    # ===================================================================
    # ПОДІЇ
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
                    log.debug("subscriber збій", exc_info=True)

    def _wants(self, name: str) -> bool:
        return any(name in w for w in self.subscribers.values())

    # ===================================================================
    # ЦИКЛИ
    # ===================================================================
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.scan()
        self._tasks = [asyncio.create_task(self._scan_loop(), name="scan"),
                       asyncio.create_task(self._tick_loop(), name="tick")]

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
            s.stop()          # у asyncio-потоці: remove_reader/cancel таймерів не потокобезпечні
        self.sessions.clear()

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.poll_interval_s)
            try:
                await self.scan()
            except Exception:  # noqa: BLE001
                log.exception("помилка hotplug-опитування")

    async def _tick_loop(self) -> None:
        """Живий стан для Web UI: ~ui_hz кадрів/с, лише змінені; stats — раз на секунду."""
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
    # ВИЯВЛЕННЯ ТА ВІДКРИТТЯ
    # ===================================================================
    def pick_layout(self, info: DeviceInfo) -> Optional[DeviceLayout]:
        """Явні «overrides» із daemon.json мають пріоритет, далі — збіг VID/PID(/interface);
        конкретніші (з вказаним interface) попереду загальних."""
        if info.backend == "evdev":
            return self._auto_layout(info)      # evdev-пристрій має власний (синтетичний) формат звіту
        for o in self.cfg.overrides:
            try:
                if (int(str(o["vid"]), 16), int(str(o["pid"]), 16)) == (info.vid, info.pid) and \
                        o.get("layout") in self.layouts:
                    if o.get("interface") is None or o["interface"] == info.interface:
                        return self.layouts[o["layout"]]
            except (KeyError, ValueError):
                continue
        cands = [l for l in self.layouts.values() if l.matches(info.vid, info.pid, info.interface, info.bus)]
        cands.sort(key=lambda l: l.interface is None)
        if cands:
            return cands[0]
        return self._auto_layout(info)          # рівень 2: розкладка з HID-дескриптора

    def _auto_layout(self, info: DeviceInfo) -> Optional[DeviceLayout]:
        """Складає розкладку з дескриптора невідомого пристрою (з кешем).

        pick_layout() викликається щосекунди (hotplug-опитування) і при кожному list_devices(),
        тож розбір дескриптора виконується один раз на (VID, PID, інтерфейс, дескриптор);
        «негативний» результат (не геймпад, напр. клавіатурний інтерфейс ресивера) теж кешується.
        """
        if not self.cfg.auto_layout or not (info.descriptor or info.extra):
            return None
        # Якщо для цього VID/PID уже є розкладка на ІНШОМУ інтерфейсі (напр. Flydigi: дані йдуть
        # через вендорський інтерфейс 2, а геймпадний інтерфейс 0 мовчить), авто-розкладку для
        # решти інтерфейсів не робимо: інакше вийде другий «геймпад»-дублікат і зайвий EVIOCGRAB.
        if info.backend != "evdev" and any(l.vid == info.vid and info.pid in l.pids
                                           for l in self.layouts.values()):
            return None
        key = (info.vid, info.pid, info.interface, info.backend, layout_key(info))
        if key not in self._auto:
            lay = try_layout(info)
            self._auto[key] = lay
            if lay is not None:
                log.info("%04x:%04x iface=%s: готової розкладки немає — складено з HID-дескриптора (%s). "
                         "Ролі кнопок/осей — евристика, перевірте: gamepad-remapd autolayout --save",
                         info.vid, info.pid, info.interface, lay.id)
        return self._auto[key]

    def all_layouts(self) -> Dict[str, DeviceLayout]:
        """Розкладки з диска + автоматично складені (для IPC/UI та open_device)."""
        out = dict(self.layouts)
        for lay in self._auto.values():
            if lay is not None:
                out.setdefault(lay.id, lay)
        return out

    def visible_devices(self) -> List[DeviceInfo]:
        """Перелік пристроїв без evdev-«дублів» тих, що вже читаються через hidraw із розкладкою."""
        return drop_shadowed_evdev(self._enumerate(self.cfg.backend), lambda i: self.pick_layout(i) is not None)

    async def scan(self) -> None:
        infos = await self.loop.run_in_executor(None, self.visible_devices)
        current = {device_id(i): i for i in infos}
        changed = set(current) != set(self.known)
        self.known = current
        self._declined &= set(current)            # зник -> наступного разу відкриється знову
        for sid in [k for k in self.failed if k not in current]:
            del self.failed[sid]
        if self.cfg.auto_open:
            for sid, info in current.items():
                if sid in self.sessions or sid in self._opening or sid in self._declined:
                    continue
                lay = self.pick_layout(info)
                if lay is None:
                    continue
                f = self.failed.get(sid)
                if f and time.monotonic() - f[0] < RETRY_FAILED_S:
                    continue
                try:
                    await self._open(info, lay)
                except ManagerError:
                    # Причину вже залоговано й збережено у self.failed (з паузою перед повтором).
                    # Один «поганий» пристрій (напр. PermissionError без udev-правил) не повинен
                    # ні валити старт демона, ні блокувати відкриття решти пристроїв.
                    continue
        if changed:
            self.emit({"event": "devices", "devices": self.list_devices()})

    async def _open(self, info: DeviceInfo, layout: Optional[DeviceLayout]) -> Session:
        sid = device_id(info)
        if sid in self.sessions:
            raise ManagerError(f"{sid}: уже відкрито")
        self._opening.add(sid)
        s = Session(info, layout, self.cfg, self.store, open_fn=self._open_fn,
                    output_fn=self._output_fn, on_dead=self._thread_dead)
        try:
            await self.loop.run_in_executor(None, s.prepare)   # open + handshake + uinput
            s.activate(self.loop)
        except Exception as e:  # noqa: BLE001
            self.failed[sid] = (time.monotonic(), f"{type(e).__name__}: {e}")
            level = logging.WARNING if isinstance(e, PermissionError) else logging.ERROR
            log.log(level, "%s: не вдалося відкрити: %s%s", sid, e,
                    " (перевірте udev-правила: packaging/linux/udev)" if isinstance(e, PermissionError) else "")
            raise ManagerError(f"{sid}: {e}") from e
        finally:
            self._opening.discard(sid)
        self.failed.pop(sid, None)
        self.sessions[sid] = s
        log.info("%s: сесія запущена (%s, розкладка=%s, профіль=%s)",
                 sid, info.name or "?", layout.id if layout else "—", s.profile_name)
        self.emit({"event": "devices", "devices": self.list_devices()})
        return s

    def _thread_dead(self, s: Session) -> None:
        """Викликається з потоку-читача — переходимо в asyncio-потік."""
        loop = self.loop
        if loop is not None and not self._stopping and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._reap(s)))

    async def _reap(self, s: Session) -> None:
        if self.sessions.get(s.id) is s:
            del self.sessions[s.id]
            s.stop()          # потік-читач уже завершується: join() миттєвий
            self.failed[s.id] = (time.monotonic(), s.error or "завершено")
            self.emit({"event": "devices", "devices": self.list_devices()})

    # ===================================================================
    # API ДЛЯ CONTROL-СЕРВЕРА
    # ===================================================================
    def get(self, sid: Optional[str]) -> Session:
        if sid is None:
            if len(self.sessions) == 1:
                return next(iter(self.sessions.values()))
            raise ManagerError("вкажіть device (відкрито сесій: %d)" % len(self.sessions))
        try:
            return self.sessions[sid]
        except KeyError:
            raise ManagerError(f"пристрій {sid!r} не відкрито") from None

    def list_devices(self) -> List[Dict[str, Any]]:
        out = []
        for sid, info in self.known.items():
            s = self.sessions.get(sid)
            if s is not None:
                out.append(s.describe())
                continue
            lay = self.pick_layout(info)
            d = {**info.as_dict(), "id": sid, "open": False,
                 "layout": lay.id if lay else None, "layout_name": lay.name if lay else None,
                 "error": (self.failed.get(sid) or (0, None))[1]}
            out.append(d)
        return out

    async def open_device(self, sid: str, layout_id: Optional[str]) -> Dict[str, Any]:
        info = self.known.get(sid)
        if info is None:
            raise ManagerError(f"пристрій {sid!r} не знайдено")
        lay = None
        if layout_id:
            lay = self.all_layouts().get(layout_id)
            if lay is None:
                raise ManagerError(f"розкладку {layout_id!r} не знайдено")
        self._declined.discard(sid)
        if sid in self.sessions:                  # перевідкрити з іншою розкладкою
            await self.close_device(sid, decline=False)
        s = await self._open(info, lay)
        return s.describe()

    async def close_device(self, sid: str, decline: bool = True) -> None:
        s = self.get(sid)
        del self.sessions[sid]
        if decline:
            self._declined.add(sid)               # не відкривати автоматично, доки не зникне/не відкриють вручну
        s.stop()              # wake() будить poll(), join() триває мілісекунди
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


__all__ = ["Manager", "ManagerError", "ProfileError", "StoreError"]
