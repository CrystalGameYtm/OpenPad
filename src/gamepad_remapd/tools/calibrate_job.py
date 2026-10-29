"""Фонове завдання «майстер калібрування»: відкриває пристрій, будить його, годує Calibrator.

    gamepad-remapd calibrate /dev/hidraw10 --save

Конвеєр для будь-якого hidraw-інтерфейсу, навіть невідомого:
  1. якщо для пристрою вже є розкладка з handshake (Flydigi V1 тощо) — виконуємо його;
  2. слухаємо 1 с. Звіти йдуть — handshake не потрібен;
  3. мовчить -> перебираємо кандидатів бібліотеки handshake (байти з SDL: Flydigi, 8BitDo,
     GameSir, Switch ...), як у `handshake-probe`; перший, після якого потік ожив, запам'ятовуємо
     і запишемо в розкладку разом із результатами калібрування;
  4. Calibrator вчить кнопки й осі зі звітів, користувач проходить кроки.

Нічого не пишемо в прошивку: лише кроки з бібліотеки (scope any/vendor, а opt-in чужих виробників —
лише з broad=True).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..input import DeviceGone, open_device, run_handshake
from ..logic.autolayout import try_layout
from ..logic.calibrate import Calibrator, default_plan, Item
from ..logic.handshake_lib import BUILTIN_LIBRARY, Candidate, candidates_for, load_library
from ..logic.layout import DeviceLayout, HandshakeStep

log = logging.getLogger(__name__)

LISTEN_S = 1.0           # скільки слухати перед handshake
WAKE_WINDOW_S = 1.5      # скільки чекати відповіді на кожен handshake
BUS_NAMES = {3: "usb", 5: "bluetooth"}


def step_to_json(s: HandshakeStep) -> dict:
    if s.op == "sleep_ms":
        return {"op": "sleep_ms", "ms": s.ms}
    if s.op == "feature_get":
        return {"op": "feature_get", "report_id": s.report_id, "length": s.length}
    return {"op": s.op, "data": " ".join(f"{b:02x}" for b in s.data)}


class CalibJob:
    def __init__(self, info, layouts: Optional[Dict[str, DeviceLayout]] = None,
                 library: Optional[List[Candidate]] = None, *, open_fn: Callable = open_device,
                 broad: bool = False, plan: Optional[List[Item]] = None, baseline_s: float = 3.0,
                 listen_s: float = LISTEN_S, wake_s: float = WAKE_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic):
        if info.backend == "evdev":
            raise ValueError("evdev-пристрої вже мають стандартні коди кнопок: калібрування не потрібне")
        self.info, self.layouts = info, layouts or {}
        self.library = library if library is not None else load_library(BUILTIN_LIBRARY)
        self.open_fn, self.broad = open_fn, broad
        self.listen_s, self.wake_s = listen_s, wake_s
        self.clock = clock                                  # час звітів для Calibrator (у тестах — модельний)
        self.cal = Calibrator(info.vid, info.pid, info.name or "", info.interface, plan=plan, baseline_s=baseline_s)
        self.lock = threading.Lock()
        self.state = "starting"
        self.note = ""
        self.handshake_id: Optional[str] = None
        self.stop_ev = threading.Event()
        self.dev = None
        self.thread = threading.Thread(target=self._run, name="calibrate", daemon=True)

    # ---- керування ----
    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_ev.set()
        dev = self.dev
        if dev is not None:
            try:
                dev.wake()
            except Exception:  # noqa: BLE001
                pass

    def join(self, timeout: float = 3.0) -> None:
        self.thread.join(timeout)

    @property
    def active(self) -> bool:
        return self.state in ("starting", "listening", "waking", "running")

    def skip(self) -> None:
        with self.lock:
            self.cal.skip()

    def finish_step(self) -> None:
        with self.lock:
            self.cal.finish_step()

    def conflicts(self) -> List[str]:
        """Розбіжності результату з уже відомою розкладкою цього ж пристрою (вбудованою за VID/PID або
        складеною з HID-дескриптора). Це не обов'язково помилка (клон може відрізнятись), але саме так
        видно, що майстер схибив: наприклад осі в зоні гіроскопа замість байтів 1–4."""
        with self.lock:
            if not self.cal.finished:
                return []
            mine = DeviceLayout.from_dict(self.cal.build(), "tmp")
        refs = []
        for lay in self.layouts.values():
            if lay.priority == 0 and lay.matches(self.info.vid, self.info.pid, self.info.interface, self.info.bus):
                refs.append((f"розкладка {lay.id}", lay))
        try:
            auto = try_layout(self.info)
            if auto is not None:
                refs.append(("HID-дескриптор пристрою", auto))
        except Exception:  # noqa: BLE001
            pass
        out: List[str] = []
        for src, ref in refs[:2]:
            if ref.report_id is not None and mine.report_id is not None and ref.report_id != mine.report_id:
                continue                                    # інший формат звіту: порівнювати нічого
            for name, f in mine.fields.items():
                r = ref.fields.get(name)
                if r is not None and (r.offset, r.fmt) != (f.offset, f.fmt):
                    out.append(f"{name}: калібрування — байт {f.offset} ({f.fmt}), {src} — байт {r.offset} ({r.fmt})")
            for name, b in mine.buttons.items():
                r = ref.buttons.get(name)
                if r is not None and (r.byte, r.bit) != (b.byte, b.bit):
                    out.append(f"{name}: калібрування — байт {b.byte} біт {b.bit}, {src} — байт {r.byte} біт {r.bit}")
        return out[:12]

    def status(self) -> dict:
        conflicts = self.conflicts()
        with self.lock:
            st = self.cal.status()
        if conflicts:
            st["warnings"] = st["warnings"] + ["Розбіжності з відомою розкладкою: " + "; ".join(conflicts)]
        st.update(state=self.state, note=self.note, handshake=self.handshake_id,
                  path=self.info.path, name=self.info.name)
        return st

    # ---- результат ----
    def result_layout(self) -> dict:
        with self.lock:
            d = self.cal.build()
            issues = self.cal.verify()
        bus = BUS_NAMES.get(getattr(self.info, "bus", None))
        if bus:
            d["match"]["bus"] = bus
        conflicts = self.conflicts()
        if conflicts:
            issues = issues + ["розбіжності з відомою розкладкою: " + "; ".join(conflicts)]
        if issues:
            d.setdefault("_learned", {})["verify"] = issues
        return d

    def save(self, devices_dir: Path) -> Path:
        from ..store import _atomic_write
        if not self.cal.finished:
            raise ValueError("майстер ще не завершено")
        d = self.result_layout()
        if not d.get("buttons") and not d.get("fields") and not d.get("hat"):
            raise ValueError("нічого не розпізнано: зберігати нічого")
        lid = self.cal.layout().id
        DeviceLayout.from_dict(d, lid)                      # LayoutError, якщо не завантажиться
        dest = Path(devices_dir) / f"{lid}.json"
        _atomic_write(dest, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        return dest

    # ---- потік ----
    def _existing_handshake(self) -> List[dict]:
        for lay in self.layouts.values():
            if lay.handshake and lay.matches(self.info.vid, self.info.pid, self.info.interface, self.info.bus):
                return [step_to_json(s) for s in lay.handshake]
        return []

    def _collect(self, seconds: float, accept: Callable[[bytes], bool] = lambda r: True) -> int:
        """Слухає `seconds`, повертає, скільки звітів пройшло фільтр (у Calibrator ці звіти не йдуть)."""
        end, n = time.monotonic() + seconds, 0
        while time.monotonic() < end and not self.stop_ev.is_set():
            raw = self.dev.read()
            if raw is not None and accept(raw):
                n += 1
        return n

    def _wake(self) -> None:
        steps = self._existing_handshake()
        if steps:
            run_handshake(self.dev, DeviceLayout.from_dict({"match": {}, "handshake": steps}, "tmp").handshake)
            self.cal.handshake = steps
            self.handshake_id = "layout"
            self.note = "виконано handshake з розкладки пристрою"
        self.state = "listening"
        if self._collect(self.listen_s) >= 3:
            return
        self.state = "waking"
        for c in candidates_for(self.info, self.library, self.broad):
            if c.is_noop or self.stop_ev.is_set():
                continue
            self.note = f"пристрій мовчить: пробую handshake «{c.id}»"
            try:
                run_handshake(self.dev, list(c.steps))
            except DeviceGone:
                raise
            except Exception as e:  # noqa: BLE001
                log.info("calibrate: handshake %s: %s", c.id, e)
                continue
            if self._collect(self.wake_s, c.expect.matches) >= 3:
                self.cal.handshake = [dict(s) for s in c.steps_json]
                self.handshake_id = c.id
                self.note = f"пристрій ожив після handshake «{c.id}»"
                return
        self.note = "пристрій мовчить: рухайте стіки й тискайте кнопки (деякі шлють звіти лише при зміні)"

    def _run(self) -> None:
        try:
            self.dev = self.open_fn(self.info, 128)      # з запасом: звіти Bluetooth-DS4 (0x11) мають 78 байт
        except PermissionError as e:
            self.state, self.note = "error", f"немає прав на {self.info.path}: {e}. Перевірте udev/ACL."
            return
        except Exception as e:  # noqa: BLE001
            self.state, self.note = "error", f"не вдалося відкрити {self.info.path}: {e}"
            return
        try:
            self._wake()
            self.state = "running"
            while not self.stop_ev.is_set() and not self.cal.finished:
                raw = self.dev.read()
                now = self.clock()
                with self.lock:
                    if raw is None:
                        self.cal.tick(now)
                    else:
                        self.cal.feed(raw, now)
            if self.cal.finished:
                self.state = "done"
        except DeviceGone:
            self.state, self.note = "gone", "пристрій відключено"
        except Exception as e:  # noqa: BLE001
            log.exception("calibrate")
            self.state, self.note = "error", str(e)
        finally:
            try:
                self.dev.close()
            except Exception:  # noqa: BLE001
                pass


def run_cli(path: str, layouts: Dict[str, DeviceLayout], devices_dir: Path, *, backend: str = "auto",
            broad: bool = False, save: bool = False, enumerate_fn=None, open_fn=open_device,
            library=None, out=None, lines=None, paddles: bool = False) -> int:
    """Консольний майстер. Enter — «готово» для осі/тригера і «пропустити» для кнопки; `s` — пропустити.
    `lines` (ітератор рядків) — для тестів; за замовчуванням читається stdin у фоновому потоці."""
    import sys
    from ..input import enumerate_devices
    out = out or sys.stdout
    info = next((i for i in (enumerate_fn or enumerate_devices)(backend) if i.path == path), None)
    if info is None:
        print(f"пристрій {path} не знайдено", file=sys.stderr)
        return 1
    try:
        job = CalibJob(info, layouts, library, open_fn=open_fn, broad=broad,
                       plan=default_plan(paddles=True) if paddles else None)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    def pump() -> None:
        try:
            for line in (lines if lines is not None else sys.stdin):
                st = job.status()
                if line.strip().lower() == "s" or st["kind"] in ("button", "dpad"):
                    job.skip()
                else:
                    job.finish_step()
        except (OSError, ValueError):
            pass

    job.start()
    threading.Thread(target=pump, name="calibrate-stdin", daemon=True).start()
    print(f"{info.path} {info.vid:04x}:{info.pid:04x} {info.name}\n"
          "Візьміть геймпад у руки й трохи поворушіть його, але кнопок і стіків НЕ чіпайте (вимірюю шум).\n"
          "Далі: Enter — готово/пропустити, Ctrl+C — перервати.",
          file=out, flush=True)
    last = None
    try:
        while job.active:
            st = job.status()
            key = (st["state"], st["note"], st["key"], st["message"] if st["message"].startswith(("Змінилось", "Не бачу")) else "", len(st["done"]))
            if key != last:
                last = key
                if st["key"] is None and st["note"]:
                    print(f"  … {st['note']}", file=out, flush=True)
                elif st["key"]:
                    print(f"[{st['index'] + 1}/{st['total']}] {st['prompt']}", file=out, flush=True)
                    if st["message"].startswith(("Змінилось", "Не бачу")):
                        print(f"    ! {st['message']}", file=out, flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        job.stop()
        return 130
    job.join()
    st = job.status()
    print(("\n" + st["message"]) if st["state"] == "done" else ("\nЗупинено: " + st["note"]), file=out)
    for w in st["warnings"]:
        print(f"  ⚠ {w}", file=out)
    if st["state"] != "done":
        return 1
    if save:
        try:
            print(f"збережено: {job.save(devices_dir)}", file=out)
        except (ValueError, OSError) as e:
            print(f"не збережено: {e}", file=sys.stderr)
            return 1
    return 0
