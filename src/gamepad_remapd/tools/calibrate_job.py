"""Background job "calibration wizard": opens the device, wakes it up, feeds the Calibrator.

    gamepad-remapd calibrate /dev/hidraw10 --save

Pipeline for any hidraw interface, even an unknown one:
  1. if the device already has a layout with a handshake (Flydigi V1 etc.) — run it;
  2. listen for 1 s. If reports arrive — no handshake is needed;
  3. if silent -> iterate over the handshake library candidates (bytes from SDL: Flydigi, 8BitDo,
     GameSir, Switch ...), as in `handshake-probe`; the first one after which the stream came alive is remembered
     and written into the layout together with the calibration results;
  4. the Calibrator learns buttons and axes from the reports, the user goes through the steps.

We write nothing to the firmware: only steps from the library (scope any/vendor; opt-in third-party packets only with broad=True).
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
from ..i18n import tr

log = logging.getLogger(__name__)

LISTEN_S = 1.0           
WAKE_WINDOW_S = 1.5      
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
        if info.backend in ("evdev", "xusb"):
            raise ValueError(tr('evdev devices already have standard button codes: no calibration needed'))
        self.info, self.layouts = info, layouts or {}
        self.library = library if library is not None else load_library(BUILTIN_LIBRARY)
        self.open_fn, self.broad = open_fn, broad
        self.listen_s, self.wake_s = listen_s, wake_s
        self.clock = clock                                  
        self.cal = Calibrator(info.vid, info.pid, info.name or "", info.interface, plan=plan, baseline_s=baseline_s)
        self.lock = threading.Lock()
        self.state = "starting"
        self.note = ""
        self.handshake_id: Optional[str] = None
        self.stop_ev = threading.Event()
        self.dev = None
        self.thread = threading.Thread(target=self._run, name="calibrate", daemon=True)

    # ---- control ----
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
        """Differences between the result and the already known layout of the same device (built in by VID/PID or
        built from the HID descriptor). This is not necessarily an error (a clone may differ), but this is how
        you can see that the wizard made a mistake: for example axes in the gyro zone instead of bytes 1–4.
        """
        with self.lock:
            if not self.cal.finished:
                return []
            mine = DeviceLayout.from_dict(self.cal.build(), "tmp")
        refs = []
        for lay in self.layouts.values():
            if lay.priority == 0 and lay.matches(self.info.vid, self.info.pid, self.info.interface, self.info.bus):
                refs.append((tr('layout {0}', lay.id), lay))
        try:
            auto = try_layout(self.info)
            if auto is not None:
                refs.append((tr('device HID descriptor'), auto))
        except Exception:  # noqa: BLE001
            pass
        out: List[str] = []
        for src, ref in refs[:2]:
            if ref.report_id is not None and mine.report_id is not None and ref.report_id != mine.report_id:
                continue                                    
            for name, f in mine.fields.items():
                r = ref.fields.get(name)
                if r is not None and (r.offset, r.fmt) != (f.offset, f.fmt):
                    out.append(tr('{0}: calibration — byte {1} ({2}), {3} — byte {4} ({5})', name, f.offset, f.fmt, src, r.offset, r.fmt))
            for name, b in mine.buttons.items():
                r = ref.buttons.get(name)
                if r is not None and (r.byte, r.bit) != (b.byte, b.bit):
                    out.append(tr('{0}: calibration — byte {1} bit {2}, {3} — byte {4} bit {5}', name, b.byte, b.bit, src, r.byte, r.bit))
        return out[:12]

    def status(self) -> dict:
        conflicts = self.conflicts()
        with self.lock:
            st = self.cal.status()
        if conflicts:
            st["warnings"] = st["warnings"] + [tr('Differences from the known layout: ') + "; ".join(conflicts)]
        st.update(state=self.state, note=self.note, handshake=self.handshake_id,
                  path=self.info.path, name=self.info.name)
        return st

    # ---- result ----
    def result_layout(self) -> dict:
        with self.lock:
            d = self.cal.build()
            issues = self.cal.verify()
        bus = BUS_NAMES.get(getattr(self.info, "bus", None))
        if bus:
            d["match"]["bus"] = bus
        conflicts = self.conflicts()
        if conflicts:
            issues = issues + [tr('differences from the known layout: ') + "; ".join(conflicts)]
        if issues:
            d.setdefault("_learned", {})["verify"] = issues
        return d

    def save(self, devices_dir: Path) -> Path:
        from ..store import _atomic_write
        if not self.cal.finished:
            raise ValueError(tr('the wizard is not finished yet'))
        d = self.result_layout()
        if not d.get("buttons") and not d.get("fields") and not d.get("hat"):
            raise ValueError(tr('nothing was recognized: nothing to save'))
        lid = self.cal.layout().id
        DeviceLayout.from_dict(d, lid)                      # LayoutError if it does not load
        dest = Path(devices_dir) / f"{lid}.json"
        _atomic_write(dest, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        return dest

    # ---- stream ----
    def _existing_handshake(self) -> List[dict]:
        for lay in self.layouts.values():
            if lay.handshake and lay.matches(self.info.vid, self.info.pid, self.info.interface, self.info.bus):
                return [step_to_json(s) for s in lay.handshake]
        return []

    def _collect(self, seconds: float, accept: Callable[[bytes], bool] = lambda r: True) -> int:
        """Listens for `seconds`, returns how many reports passed the filter (these reports are not fed to the Calibrator)."""
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
            self.note = tr("the handshake from the device's layout was performed")
        self.state = "listening"
        if self._collect(self.listen_s) >= 3:
            return
        self.state = "waking"
        for c in candidates_for(self.info, self.library, self.broad):
            if c.is_noop or self.stop_ev.is_set():
                continue
            self.note = tr('the device is silent: trying the handshake “{0}”', c.id)
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
                self.note = tr('the device came alive after the handshake “{0}”', c.id)
                return
        self.note = tr('the device is silent: move the sticks and press buttons (some send reports only on change)')

    def _run(self) -> None:
        try:
            self.dev = self.open_fn(self.info, 128)      
        except PermissionError as e:
            self.state, self.note = "error", tr('no permission for {0}: {1}. Check udev/ACL.', self.info.path, e)
            return
        except Exception as e:  # noqa: BLE001
            self.state, self.note = "error", tr('could not open {0}: {1}', self.info.path, e)
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
            self.state, self.note = "gone", tr('device disconnected')
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
    """Console wizard. Enter — "done" for an axis/trigger and "skip" for a button; `s` — skip.
        `lines` (an iterator of lines) — for tests; by default stdin is read in a background thread.
    """
    import sys
    from ..input import enumerate_devices
    out = out or sys.stdout
    info = next((i for i in (enumerate_fn or enumerate_devices)(backend) if i.path == path), None)
    if info is None:
        print(tr('device {0} not found', path), file=sys.stderr)
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
    print(tr('{0} {1:04x}:{2:04x} {3}\n'
             'Pick up the gamepad and move it around a little, but do NOT touch the buttons or sticks (I am measuring the noise).\n'
             'Next: Enter — done/skip, Ctrl+C — abort.', info.path, info.vid, info.pid, info.name),
          file=out, flush=True)
    last = None
    try:
        while job.active:
            st = job.status()
            key = (st["state"], st["note"], st["key"], st["message"] if st["message"].startswith((tr('Changed'), tr('Not seen'))) else "", len(st["done"]))
            if key != last:
                last = key
                if st["key"] is None and st["note"]:
                    print(f"  … {st['note']}", file=out, flush=True)
                elif st["key"]:
                    print(f"[{st['index'] + 1}/{st['total']}] {st['prompt']}", file=out, flush=True)
                    if st["message"].startswith((tr('Changed'), tr('Not seen'))):
                        print(f"    ! {st['message']}", file=out, flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        job.stop()
        return 130
    job.join()
    st = job.status()
    print(("\n" + st["message"]) if st["state"] == "done" else (tr('\nStopped: ') + st["note"]), file=out)
    for w in st["warnings"]:
        print(f"  ⚠ {w}", file=out)
    if st["state"] != "done":
        return 1
    if save:
        try:
            print(tr('saved: {0}', job.save(devices_dir)), file=out)
        except (ValueError, OSError) as e:
            print(tr('not saved: {0}', e), file=sys.stderr)
            return 1
    return 0
