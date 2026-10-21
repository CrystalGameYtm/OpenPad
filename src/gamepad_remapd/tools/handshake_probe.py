"""Автопошук handshake: пробує відомі послідовності з бібліотеки й дивиться, чи ожив потік звітів.

Один прохід по кандидатах; для кожного: handshake -> вікно збору звітів -> оцінка. Успіх, якщо
пристрій

  * шле потік сам (≥10 звітів за вікно) — так поводяться Flydigi V1 з IMU, Switch Pro ...; або
  * реагує на користувача (≥3 звіти, що змінюються) — для геймпадів, які шлють стан лише при зміні.

Другий критерій і є «сигнал від користувача»: просимо натискати кнопки й рухати стіки ПРОТЯГОМ
усього пошуку. Це важливо для достовірності: стан handshake лишається в пристрої, тож вікно кожного
кандидата мусить іти одразу після його власного handshake, а активність користувача — перекривати
всі вікна. Тоді «розбудив» саме той кандидат, на якому звіти з'явились. (Окремі фази «спершу без
користувача, потім з ним» давали б хибне авторство: тихо розбуджений першою фазою пристрій
відповів би вже наступному кандидатові.) Режим без користувача (`use_user=False`) лишає тільки
перший критерій.

Handshake зберігається лише після підтвердження користувача (`save_learned`).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, TextIO

from ..input import DeviceGone, open_device, run_handshake
from ..logic.handshake_lib import (Candidate, bind_layout, candidates_for, layout_match_fraction,
                                   learned_layout_id, load_library)
from ..logic.layout import DeviceLayout

log = logging.getLogger(__name__)

AUTO_MIN_REPORTS = 10          # автономний потік: стільки звітів за вікно
USER_MIN_REPORTS = 3           # реакція на користувача: стільки звітів...
USER_MIN_DISTINCT = 2          # ...і принаймні 2 різні (значення змінюються)
LAYOUT_MATCH_MIN = 0.5         # частка звітів, що проходять фільтр розкладки-кандидата


@dataclass
class ProbeResult:
    ok: bool = False
    candidate: Optional[str] = None
    name: str = ""
    mode: str = ""                       # handshake | no-handshake
    reports: int = 0
    rate_hz: float = 0.0
    report_len: int = 0
    samples: List[str] = field(default_factory=list)
    layout: Optional[str] = None         # id готової розкладки потоку (якщо підійшла)
    layout_match: float = 0.0
    tried: List[dict] = field(default_factory=list)
    message: str = ""
    error: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def collect(dev, seconds: float, stop: threading.Event) -> List[bytes]:
    """Усі звіти за `seconds` (блокуюче read() будить таймер). Службові відповіді теж повертаються —
    фільтр робить викликач."""
    out: List[bytes] = []
    end = time.monotonic() + seconds
    timer = threading.Timer(seconds, dev.wake)
    timer.daemon = True
    timer.start()
    try:
        while not stop.is_set() and time.monotonic() < end:
            raw = dev.read()
            if raw:
                out.append(bytes(raw))
    finally:
        timer.cancel()
    return out


class HandshakeProbe:
    def __init__(self, info, library: Optional[List[Candidate]] = None, *,
                 layouts: Optional[Dict[str, DeviceLayout]] = None, broad: bool = False,
                 open_fn: Callable = open_device, emit: Callable[[dict], None] = lambda ev: None,
                 auto_s: float = 1.2, user_s: float = 2.5, grace_s: float = 2.0, use_user: bool = True):
        self.info, self.layouts = info, layouts or {}
        self.library = library if library is not None else load_library()
        self.broad, self.open_fn, self.emit = broad, open_fn, emit
        self.auto_s, self.user_s, self.grace_s, self.use_user = auto_s, user_s, grace_s, use_user
        self.stop_event = threading.Event()
        self._dev = None
        self.result: Optional[ProbeResult] = None

    def stop(self) -> None:
        self.stop_event.set()
        dev = self._dev
        if dev is not None:
            try:
                dev.wake()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def run(self) -> ProbeResult:
        cands = candidates_for(self.info, self.library, self.broad)
        res = ProbeResult()
        self.result = res
        if self.info.backend == "evdev":
            return self._fail(res, "evdev-пристрої не потребують handshake (їх ініціалізує ядро)", error=True)
        if not cands:
            return self._fail(res, "для цього пристрою немає кандидатів (спробуйте «широкий перебір»)", error=True)
        self.emit({"kind": "start", "total": len(cands), "ids": [c.id for c in cands], "broad": self.broad})
        window = self.user_s if self.use_user else self.auto_s
        phase = "user" if self.use_user else "auto"
        try:
            if self.use_user:
                self.emit({"kind": "prompt", "text": "Протягом усього пошуку НАТИСКАЙТЕ кнопки й рухайте стіки "
                                                     "(це ≈ 10–30 с). Пристрій, що мовчить у спокої, виявить себе саме так."})
                if self._sleep(self.grace_s):
                    return self._fail(res, "зупинено", error=False)
            for i, c in enumerate(cands):
                if self.stop_event.is_set():
                    return self._fail(res, "зупинено", error=False)
                self.emit({"kind": "try", "phase": phase, "index": i + 1, "total": len(cands),
                           "id": c.id, "name": c.name})
                reports = self._attempt(c, window)
                if reports is None:
                    return self._fail(res, f"{self.info.path}: немає прав на запис — потрібні udev-правила "
                                           "(gamepad-remapd udev-rules)", error=True)
                ok, flt = self._judge(c, reports, self.use_user)
                res.tried.append({"id": c.id, "reports": len(reports), "matched": len(flt), "ok": ok})
                if ok:
                    return self._success(res, c, flt, window)
            return self._fail(res, "жоден кандидат не оживив потік. Можливо, потрібного handshake немає в "
                                   "бібліотеці: спробуйте «широкий перебір» або перехоплення USB-трафіку офіційної програми.",
                              error=False)
        except PermissionError as e:
            return self._fail(res, f"немає прав на {self.info.path}: {e}", error=True)
        except DeviceGone:
            return self._fail(res, "пристрій відключено під час перебору", error=True)
        except OSError as e:
            return self._fail(res, f"помилка пристрою: {e}", error=True)

    # ------------------------------------------------------------------
    def _sleep(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds) if seconds > 0 else self.stop_event.is_set()

    def _attempt(self, c: Candidate, window: float) -> Optional[List[bytes]]:
        """Відкриває пристрій, виконує handshake кандидата й збирає звіти. None — немає прав на запис."""
        dev = self.open_fn(self.info, 64)
        self._dev = dev
        try:
            if c.steps and getattr(dev, "can_write", True) is False:
                return None
            if c.steps:
                run_handshake(dev, c.steps)
            return collect(dev, window, self.stop_event)
        finally:
            self._dev = None
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _judge(c: Candidate, reports: List[bytes], use_user: bool):
        flt = [r for r in reports if c.expect.matches(r)] if c.expect.constrained else list(reports)
        if len(flt) >= AUTO_MIN_REPORTS:
            return True, flt
        return use_user and len(flt) >= USER_MIN_REPORTS and len(set(flt)) >= USER_MIN_DISTINCT, flt

    def _success(self, res: ProbeResult, c: Candidate, flt: List[bytes], window: float) -> ProbeResult:
        res.ok, res.candidate, res.name = True, c.id, c.name
        res.mode = "no-handshake" if c.is_noop else "handshake"
        res.reports, res.rate_hz = len(flt), round(len(flt) / window, 1)
        res.report_len = Counter(len(r) for r in flt).most_common(1)[0][0]
        res.samples = [r.hex(" ") for r in list(dict.fromkeys(flt))[:5]]
        base = self.layouts.get(c.layout) if c.layout else None
        if base is not None:
            res.layout_match = round(layout_match_fraction(base, flt), 2)
            if res.layout_match >= LAYOUT_MATCH_MIN:
                res.layout = base.id
        if res.mode == "no-handshake":
            res.message = (f"Handshake не потрібен: пристрій відповідає без нього ({res.rate_hz} звітів/с, {res.report_len} байт). "
                           "Якщо цей геймпад уже «оживав» (попередній пошук, офіційна програма), вимкніть і ввімкніть "
                           "його та повторіть для надійності.")
        else:
            res.message = (f"Знайдено: {c.name} — пристрій ожив ({res.rate_hz} звітів/с, {res.report_len} байт)."
                           + (f" Розпізнано як «{res.layout}» ({int(res.layout_match * 100)}% звітів збігаються)."
                              if res.layout else " Готової розкладки для цього потоку немає — потрібне калібрування."))
        self.emit({"kind": "result", **res.to_dict()})
        return res

    def _fail(self, res: ProbeResult, message: str, error: bool) -> ProbeResult:
        res.ok, res.message, res.error = False, message, error
        self.emit({"kind": "result", **res.to_dict()})
        return res


def save_learned(res: ProbeResult, info, layouts: Dict[str, DeviceLayout], devices_dir: Path,
                 library: Optional[List[Candidate]] = None) -> Path:
    """Зберігає знайдений handshake + розкладку потоку як звичайний devices/learned_VID_PID[_ifN].json."""
    from ..store import _atomic_write
    if not res.ok or not res.candidate:
        raise ValueError("нічого зберігати: handshake не знайдено")
    if res.mode == "no-handshake":
        raise ValueError("handshake не потрібен — окрема розкладка не створюється")
    if not res.layout:
        raise ValueError("для цього потоку немає готової розкладки: спершу калібрування")
    cand = next((c for c in (library if library is not None else load_library()) if c.id == res.candidate), None)
    if cand is None:
        raise ValueError(f"кандидата {res.candidate!r} немає в бібліотеці")
    d = bind_layout(cand, info, layouts[res.layout])
    dest = Path(devices_dir) / f"{learned_layout_id(info)}.json"
    _atomic_write(dest, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    return dest


def run_cli(path: str, layouts: Dict[str, DeviceLayout], devices_dir: Path, *, backend: str = "auto",
            broad: bool = False, use_user: bool = True, save: bool = False, auto_s: float = 1.2,
            user_s: float = 2.5, enumerate_fn=None, open_fn=open_device, out: Optional[TextIO] = None,
            library: Optional[List[Candidate]] = None, grace_s: float = 2.0) -> int:
    import sys
    from ..input import enumerate_devices
    out = out or sys.stdout
    info = next((i for i in (enumerate_fn or enumerate_devices)(backend) if i.path == path), None)
    if info is None:
        print(f"пристрій {path} не знайдено", file=sys.stderr)
        return 1

    def show(ev: dict) -> None:
        k = ev["kind"]
        if k == "start":
            print(f"{info.path} {info.vid:04x}:{info.pid:04x} {info.name}: пробую {ev['total']} кандидат(ів): "
                  f"{', '.join(ev['ids'])}", file=out, flush=True)
        elif k == "prompt":
            print(f"\n>>> {ev['text']}\n", file=out, flush=True)
        elif k == "try":
            print(f"  [{ev['phase']}] {ev['index']}/{ev['total']} {ev['id']}", file=out, flush=True)
    probe = HandshakeProbe(info, library, layouts=layouts, broad=broad, open_fn=open_fn, emit=show,
                           auto_s=auto_s, user_s=user_s, use_user=use_user, grace_s=grace_s)
    try:
        res = probe.run()
    except KeyboardInterrupt:
        probe.stop()
        return 130
    print(("\nУСПІХ: " if res.ok else "\n") + res.message, file=out)
    if res.samples:
        print("зразки звітів:\n  " + "\n  ".join(res.samples), file=out)
    if res.ok and save:
        try:
            print(f"збережено: {save_learned(res, info, layouts, devices_dir, library)}", file=out)
        except (ValueError, OSError) as e:
            print(f"не збережено: {e}", file=sys.stderr)
            return 1
    return 0 if res.ok else (1 if res.error else 2)
