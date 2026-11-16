"""Automatic handshake search: tries the known sequences from the library and checks whether the report stream comes alive.

One pass over the candidates; for each: handshake -> report collection window -> evaluation. Success if the device

  * streams by itself (≥10 reports per window) — this is how Flydigi V1 with IMU, Switch Pro ... behave; or
  * reacts to the user (≥3 reports that change) — for gamepads that send state only on change.

The second criterion is the "user signal": we ask the user to press buttons and move sticks THROUGHOUT
the whole search. This matters for reliability: the handshake state stays in the device, so each candidate's
window must follow its own handshake immediately, and user activity must cover all windows. Then the candidate that
"woke" the device is exactly the one on which the reports appeared. (Separate phases "first without the user,
then with them" would give false authorship: a device quietly woken by the first phase would answer the next
candidate already.) Mode without the user (`use_user=False`) keeps only the first criterion.

The handshake is saved only after the user confirms (`save_learned`).
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
from ..i18n import tr

log = logging.getLogger(__name__)

AUTO_MIN_REPORTS = 10          
USER_MIN_REPORTS = 3           
USER_MIN_DISTINCT = 2          # ...and at least 2 different (the values change)
LAYOUT_MATCH_MIN = 0.5         


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
    layout: Optional[str] = None         # id of the ready stream layout (if it matched)
    layout_match: float = 0.0
    tried: List[dict] = field(default_factory=list)
    message: str = ""
    error: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def collect(dev, seconds: float, stop: threading.Event) -> List[bytes]:
    """All reports for `seconds` (the blocking read() wakes the timer). Service replies are returned too —
        the caller does the filtering.
    """
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
            return self._fail(res, tr('evdev devices need no handshake (the kernel initializes them)'), error=True)
        if not cands:
            return self._fail(res, tr('there are no candidates for this device (try the “broad search”)'), error=True)
        self.emit({"kind": "start", "total": len(cands), "ids": [c.id for c in cands], "broad": self.broad})
        window = self.user_s if self.use_user else self.auto_s
        phase = "user" if self.use_user else "auto"
        try:
            if self.use_user:
                self.emit({"kind": "prompt", "text": tr('Throughout the whole search PRESS buttons and move the sticks (≈ 10–30 s). A device that is silent at rest will reveal itself this way.')})
                if self._sleep(self.grace_s):
                    return self._fail(res, tr('stopped'), error=False)
            for i, c in enumerate(cands):
                if self.stop_event.is_set():
                    return self._fail(res, tr('stopped'), error=False)
                self.emit({"kind": "try", "phase": phase, "index": i + 1, "total": len(cands),
                           "id": c.id, "name": c.name})
                reports = self._attempt(c, window)
                if reports is None:
                    return self._fail(res, tr('{0}: no write permission — udev rules are needed (gamepad-remapd udev-rules)', self.info.path), error=True)
                ok, flt = self._judge(c, reports, self.use_user)
                res.tried.append({"id": c.id, "reports": len(reports), "matched": len(flt), "ok": ok})
                if ok:
                    return self._success(res, c, flt, window)
            return self._fail(res, tr('no candidate brought the stream to life. The required handshake may be missing from the library: try the “broad search” or capture the USB traffic of the official program.'),
                              error=False)
        except PermissionError as e:
            return self._fail(res, tr('no permission for {0}: {1}', self.info.path, e), error=True)
        except DeviceGone:
            return self._fail(res, tr('the device was disconnected during the search'), error=True)
        except OSError as e:
            return self._fail(res, tr('device error: {0}', e), error=True)

    # ------------------------------------------------------------------
    def _sleep(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds) if seconds > 0 else self.stop_event.is_set()

    def _attempt(self, c: Candidate, window: float) -> Optional[List[bytes]]:
        """Opens the device, performs the candidate's handshake and collects reports. None — no write permission."""
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
            res.message = (tr('No handshake needed: the device answers without it ({0} reports/s, {1} bytes). If this gamepad has already been “woken” (a previous search, the official program), turn it off and on and repeat to be sure.', res.rate_hz, res.report_len))
        else:
            res.message = (tr('Found: {0} — the device came alive ({1} reports/s, {2} bytes).', c.name, res.rate_hz, res.report_len)
                           + (tr(' Recognized as “{0}” ({1}% of the reports match).', res.layout, int(res.layout_match * 100))
                              if res.layout else tr(' There is no ready layout for this stream — calibration is needed.')))
        self.emit({"kind": "result", **res.to_dict()})
        return res

    def _fail(self, res: ProbeResult, message: str, error: bool) -> ProbeResult:
        res.ok, res.message, res.error = False, message, error
        self.emit({"kind": "result", **res.to_dict()})
        return res


def save_learned(res: ProbeResult, info, layouts: Dict[str, DeviceLayout], devices_dir: Path,
                 library: Optional[List[Candidate]] = None) -> Path:
    """Saves the found handshake + the stream layout as a regular devices/learned_VID_PID[_ifN].json."""
    from ..store import _atomic_write
    if not res.ok or not res.candidate:
        raise ValueError(tr('nothing to save: no handshake was found'))
    if res.mode == "no-handshake":
        raise ValueError(tr('no handshake is needed — a separate layout is not created'))
    if not res.layout:
        raise ValueError(tr('there is no ready layout for this stream: calibrate first'))
    cand = next((c for c in (library if library is not None else load_library()) if c.id == res.candidate), None)
    if cand is None:
        raise ValueError(tr('the candidate {0!r} is not in the library', res.candidate))
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
        print(tr('device {0} not found', path), file=sys.stderr)
        return 1

    def show(ev: dict) -> None:
        k = ev["kind"]
        if k == "start":
            print(tr('{0} {1:04x}:{2:04x} {3}: trying {4} candidate(s): {5}', info.path, info.vid, info.pid, info.name, ev['total'], ', '.join(ev['ids'])), file=out, flush=True)
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
    print((tr('\nSUCCESS: ') if res.ok else "\n") + res.message, file=out)
    if res.samples:
        print(tr('report samples:\n  ') + "\n  ".join(res.samples), file=out)
    if res.ok and save:
        try:
            print(tr('saved: {0}', save_learned(res, info, layouts, devices_dir, library)), file=out)
        except (ValueError, OSError) as e:
            print(tr('not saved: {0}', e), file=sys.stderr)
            return 1
    return 0 if res.ok else (1 if res.error else 2)
