"""Calibration wizard: learns a gamepad layout from a stream of raw reports ("on first use").

When a device has neither a ready layout nor a usable HID descriptor (the typical case —
the vendor interface of a 2.4 GHz receiver after the handshake), the report structure can be learned
from the user: "press A", "move the left stick left and right" … and observing which bits and bytes
change. The result is an ordinary layout (devices/*.json), which the same pipeline then reads.

Principles
- Only observing reports: nothing is written to the device (the handshake is performed by the caller).
- "Noisy" bytes (gyro, counters) that changed at rest do not take part in button search.
- A button = exactly ONE bit that went to 1 and stayed there for several reports; several changes at once = repeat.
- The D-pad is recognized both as bits (one bit per direction) and as a hat nibble (8 values clockwise).
- A stick/trigger axis: u8 or 16-bit (LE/BE, signed/unsigned) — determined by the smoothness of the
  value; the orientation of Y/X (up/left = smaller value) — by the direction the user moved first.
- Every step can be skipped (a button the gamepad does not have).
- The clock is passed in from outside (``now``), so the core is fully deterministic and tested without sleeping.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .layout import DeviceLayout, LayoutError, ReportParser
from ..i18n import tr

HOLD_FRAMES = 3                  
EVAL_PERIOD_S = 0.2              
MAX_AXIS_FRAMES = 1500           
SMOOTH_P90 = 0.35                # p90 |Δ| / range: a smooth axis has a value below
STICK_REACH = 0.35               
STICK_MIN_HALF_U8 = 127.5
PERSIST_S = 0.02                 
BUSY_TRANSITIONS = 3             
TRIGGER_MIN_MOVE = {1: 60, 2: 120}   


BASELINE_HINT = ('Pick up the gamepad and move it around a little (shake it, turn it), but do NOT touch the buttons, sticks or triggers: this is how I learn which bytes are “noisy” on their own.')


def _popcount(v: int) -> int:
    return bin(v).count("1")


@dataclass(frozen=True)
class Item:
    kind: str                    # button | dpad | stick | trigger
    key: str                     
    label: str
    prompt: str
    target: Optional[str] = None  
    expect_first_low: bool = False  


def default_plan(paddles: bool = False) -> List[Item]:
    """`paddles=True` adds the side/back ML and MR (not every gamepad emits them; the GPS13B in DS4 mode does not)."""
    items: List[Item] = []
    for key, tgt, label in (("A", "A", "A"), ("B", "B", "B"), ("X", "X", "X"), ("Y", "Y", "Y"),
                            ("LB", "LB", tr('LB (left bumper)')), ("RB", "RB", tr('RB (right bumper)')),
                            ("BACK", "BACK", "Back / Select"), ("START", "START", "Start / Menu"),
                            ("LS", "LS", tr('left stick press (L3)')),
                            ("RS", "RS", tr('right stick press (R3)')),
                            ("GUIDE", "GUIDE", "Home / Guide")):
        items.append(Item("button", key, label, tr('Press and release: {0}', label), tgt))
    for key, label in (("UP", tr('UP')), ("RIGHT", tr('RIGHT')), ("DOWN", tr('DOWN')), ("LEFT", tr('LEFT'))):
        items.append(Item("dpad", key, f"D-pad {label}", tr('D-pad: press and release {0}', label), key))
    extras = [("M1", "M1"), ("M2", "M2"), ("M3", "M3"), ("M4", "M4"), ("C", "C"), ("Z", "Z")]
    if paddles:
        extras += [("ML", tr('ML (side/back left)')), ("MR", tr('MR (side/back right)'))]
    for key, label in extras:
        items.append(Item("button", key, tr('extra {0}', label),
                          tr('Extra button {0} (back/programmable). If there is none — “Skip”', label), key))
    for key, label, first, low in (("LX", tr('LEFT stick, horizontal'), tr('LEFT'), True),
                                   ("LY", tr('LEFT stick, vertical'), tr('UP'), True),
                                   ("RX", tr('RIGHT stick, horizontal'), tr('LEFT'), True),
                                   ("RY", tr('RIGHT stick, vertical'), tr('UP'), True)):
        axis = tr('horizontal') if key.endswith("X") else tr('vertical')
        side = tr('right') if key.endswith("X") else tr('down')
        items.append(Item("stick", key, label,
                          tr('{0}: slowly all the way FIRST {1}, then {2}, and back to the center (move along {3} only; do not shake the gamepad)', label, first, side, axis), None, low))
    for key, label in (("L2", tr('LEFT trigger')), ("R2", tr('RIGHT trigger'))):
        items.append(Item("trigger", key, label, tr('{0}: press all the way and release', label), None))
    return items


@dataclass
class _Axis:
    offset: int
    fmt: str
    lo: int
    hi: int
    rest: int
    smooth: float
    first_side: int              # -1 = below rest first, +1 = above, 0 = undetermined
    cur: int
    width: int


class Calibrator:
    def __init__(self, vid: int, pid: int, name: str = "", interface: Optional[int] = None, *,
                 handshake: Optional[Sequence[dict]] = None, plan: Optional[List[Item]] = None,
                 baseline_s: float = 3.0, hold_frames: int = HOLD_FRAMES):
        self.vid, self.pid, self.name, self.interface = vid, pid, name, interface
        self.handshake = [dict(s) for s in (handshake or [])]
        self.plan = plan if plan is not None else default_plan()
        self.baseline_s, self.hold = baseline_s, hold_frames
        self.phase = "baseline"            # baseline | step | release | done
        self.idx = 0
        self.message = tr(BASELINE_HINT)
        self.L = 0
        # --- baseline ---
        self._t0: Optional[float] = None
        self._last_rx: Optional[float] = None
        self._base: List[bytes] = []
        self.rest: bytes = b""
        self._rest_int = 0
        self.noisy_int = 0                 
        self.noise_span: List[int] = []
        self.sync: Optional[Tuple[int, int]] = None
        # --- results ---
        self.buttons: Dict[str, Tuple[int, int, Optional[str]]] = {}     # name -> (byte, bit, target)
        self.hat: Optional[Tuple[int, int, int]] = None                  # (byte, shift, first)
        self.axes: Dict[str, dict] = {}
        self.sticks: Dict[str, List[str]] = {}
        self.triggers: Dict[str, str] = {}
        self.done_items: List[dict] = []
        self.warnings: List[str] = []
        self._claimed_int = 0
        self._claimed_bytes: set = set()
        self._byte0_changed = False
        self._seen = 0
        self._rx_times: List[float] = []
        # --- step state ---
        self._cand = 0
        self._cand_n = 0
        self._dpad: Dict[str, dict] = {}
        self._ax_frames: List[bytes] = []
        self._ax_eval_at = 0.0
        self._ax_best: Optional[_Axis] = None
        self._release_n = 0
        self._retry = False
        self.peak_frames: Dict[str, bytes] = {}
        self._step_vals: Dict[int, set] = {}      
        self._dwin: deque = deque(maxlen=64)      

    # ===================================================================
    
    # ===================================================================
    @property
    def finished(self) -> bool:
        return self.phase == "done"

    @property
    def current(self) -> Optional[Item]:
        return self.plan[self.idx] if self.phase in ("step", "release") and self.idx < len(self.plan) else None

    def status(self, now: Optional[float] = None) -> dict:
        it = self.current
        hz = 0.0
        if len(self._rx_times) >= 2 and self._rx_times[-1] > self._rx_times[0]:
            hz = round((len(self._rx_times) - 1) / (self._rx_times[-1] - self._rx_times[0]), 1)
        return {"phase": self.phase, "index": min(self.idx, len(self.plan)), "total": len(self.plan),
                "key": it.key if it else None, "kind": it.kind if it else None,
                "label": it.label if it else None,
                "prompt": (it.prompt if it else (tr('Done') if self.phase == "done" else tr(BASELINE_HINT))),
                "message": self.message, "done": list(self.done_items), "warnings": list(self.warnings),
                "reports": self._seen, "rate_hz": hz, "length": self.L, "finished": self.finished}

    def skip(self) -> None:
        """Skip the current step (there is no such button/axis)."""
        it = self.current
        if it is None:
            return
        self._record(it, tr('skipped'), ok=False)
        self._advance()

    def finish_step(self) -> None:
        """The user says "done" for an axis/trigger: we take what there is."""
        it = self.current
        if it is None or it.kind not in ("stick", "trigger"):
            return
        best = self._best_axis(force=True)
        if best is None:
            self.message = tr('I do not see this axis moving. Repeat the movement or press “Skip”.')
            return
        self._accept_axis(it, best, forced=True)

    def tick(self, now: float) -> None:
        """Called even without reports (the baseline transition for devices that send only on change)."""
        if self.phase == "baseline":
            self._maybe_finish_baseline(now)

    # ===================================================================
    
    # ===================================================================
    def feed(self, raw: bytes, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if not raw:
            return
        self._seen += 1
        self._rx_times.append(now)
        del self._rx_times[:-200]
        if self.phase == "baseline":
            if self._t0 is None:
                self._t0 = now
            self._last_rx = now
            if len(self._base) < 6000:
                self._base.append(bytes(raw))
            self._maybe_finish_baseline(now)
            return
        if self.phase == "done" or len(raw) != self.L:
            return                                    
        if raw[0] != self.rest[0]:
            self._byte0_changed = True
        d = (int.from_bytes(raw, "little") ^ self._rest_int) & self._mask()
        self._dwin.append(d)
        if d and self.phase in ("step", "release"):
            for i, v in enumerate(d.to_bytes(self.L, "little")):
                if v:
                    self._step_vals.setdefault(i, set()).add(v)       
        it = self.current
        if it is None:
            return
        if self.phase == "release":
            self._on_release(raw)
        elif it.kind in ("button", "dpad"):
            self._on_press(it, raw)
        else:
            self._on_axis(it, raw, now)

    # ===================================================================
    # BASELINE
    # ===================================================================
    def _maybe_finish_baseline(self, now: float) -> None:
        if self._t0 is None or not self._base:
            return
        elapsed = now - self._t0
        streaming = len(self._base) >= 10
        silent_for = now - (self._last_rx or now)
        if elapsed < self.baseline_s:
            return
        if not streaming and silent_for < 1.0:
            return          
        lens = Counter(len(r) for r in self._base)
        self.L = lens.most_common(1)[0][0]
        frames = [r for r in self._base if len(r) == self.L]
        L = self.L
        rest = bytearray(L)
        noise = [0] * L
        noisy = 0
        for i in range(L):
            col = [f[i] for f in frames]
            rest[i] = Counter(col).most_common(1)[0][0] if streaming else col[-1]
            noise[i] = max(col) - min(col)
            if len(set(col)) > 1:
                noisy |= self._noisy_bits(col) << (8 * i)
        self.rest, self._rest_int = bytes(rest), int.from_bytes(rest, "little")
        self.noisy_int, self.noise_span = noisy, noise
        # sync: same Report ID, but a minor group of reports with a different second byte (handshake replies)
        if L >= 3 and len({f[0] for f in frames}) == 1:
            c1 = Counter(f[1] for f in frames)
            top, n = c1.most_common(1)[0]
            if len(c1) > 1 and n / len(frames) >= 0.8:
                self.sync = (1, top)
        self.phase = "step"
        self.idx = 0
        self._begin_step()
        n_noisy = sum(1 for v in noise if v)
        self.message = (tr('Done: report of {0} bytes, {1} reports at rest{2}.', L, len(frames), tr(', noisy bytes ') + str(n_noisy) if n_noisy else ''))
        if streaming and len(frames) >= 2:
            span = frames and (self._rx_times[-1] - self._rx_times[0])
            if span > 0:
                pass

    @staticmethod
    def _noisy_bits(col: List[int]) -> int:
        """A mask of the noisy bits of a byte. Usually the whole byte (gyro). The exception is a report counter in the
        high bits (DS4: byte 7 = counter in bits 2..7 + PS/touchpad buttons in bits 0..1): then we exclude only the
        counter bits, so the buttons in the low bits can still be learned.
        """
        changed = 0
        for a, b in zip(col, col[1:]):
            changed |= a ^ b
        lo = (changed & -changed).bit_length() - 1 if changed else 0
        if lo >= 1 and changed == (0xFF >> lo) << lo:
            size = 8 - lo
            mod = 1 << size
            steps = [((b >> lo) - (a >> lo)) % mod for a, b in zip(col, col[1:])]
            if steps and sum(1 for s in steps if s in (0, 1)) >= 0.95 * len(steps):
                return changed
        return 0xFF

    # ===================================================================
    
    # ===================================================================
    def _begin_step(self) -> None:
        self._step_vals = {}
        self._cand = 0
        self._cand_n = 0
        self._ax_frames = []
        self._ax_best = None
        self._ax_eval_at = 0.0
        self._release_n = 0
        self._skip_dpad_if_resolved()

    def _skip_dpad_if_resolved(self) -> None:
        pass

    def _advance(self) -> None:
        self.idx += 1
        self._retry = False
        while self.idx < len(self.plan) and self._skippable(self.plan[self.idx]):
            self.idx += 1
        if self.idx >= len(self.plan):
            self._finish()
            return
        self.phase = "step"
        self._begin_step()

    def _skippable(self, it: Item) -> bool:
        return False

    def _finish(self) -> None:
        self._resolve_dpad_if_needed()
        self.phase = "done"
        n_btn = len(self.buttons) + (4 if self.hat else 0)
        self.message = (tr('Calibration finished: {0} buttons, {1} axes.', n_btn, len(self.axes)))
        if not self.buttons and not self.hat and not self.axes:
            self.warnings.append(tr('Nothing could be recognized: the layout will be empty.'))

    def _record(self, it: Item, result: str, ok: bool) -> None:
        self.done_items = [d for d in self.done_items if d["key"] != it.key] + \
            [{"key": it.key, "label": it.label, "result": result, "ok": ok}]

    # ---- buttons / D-pad ----
    def _mask(self) -> int:
        return ~(self.noisy_int | self._claimed_int)

    def _hist_len(self) -> int:
        """How many reports correspond to PERSIST_S at the current rate (at least 3)."""
        t = self._rx_times
        hz = (len(t) - 1) / (t[-1] - t[0]) if len(t) >= 2 and t[-1] > t[0] else 125.0
        return max(3, min(30, round(hz * PERSIST_S)))

    def _persist(self) -> int:
        """Bits that differ from rest IN EACH of the last H reports and lie in the "calm" bytes.

                A button press is a bit that changes steadily. Noise (accelerometer, gyro, touchpad waking up when
                the gamepad is picked up) is bytes that flicker differently each time: the transition counter in the window
                filters them out. Because of this, several "changed bits" next to a press no longer spoil the recognition.
        """
        H = self._hist_len()
        w = self._dwin
        if len(w) < H:
            return 0
        p = w[-1]
        for k in range(2, H + 1):
            p &= w[-k]
            if not p:
                return 0
        n = min(len(w), 2 * H + 1)
        frames = [w[-k] for k in range(n, 0, -1)]
        cnt = [0] * self.L
        for x, y in zip(frames, frames[1:]):
            z = x ^ y
            if z:
                for i, v in enumerate(z.to_bytes(self.L, "little")):
                    if v:
                        cnt[i] += 1
        busy = 0
        for i, c in enumerate(cnt):
            if c > BUSY_TRANSITIONS:
                busy |= 0xFF << (8 * i)
        
        # 1 value (D-pad hat: up to 4). A byte that took more — drifts smoothly (accelerometer tilting from
        
        it = self.current
        limit = 4 if (it is not None and it.kind == "dpad") else 1
        for i, vals in self._step_vals.items():
            if len(vals) > limit:
                busy |= 0xFF << (8 * i)
                self.noisy_int |= 0xFF << (8 * i)      
        return p & ~busy & self._mask()

    def _on_press(self, it: Item, raw: bytes) -> None:
        d = self._persist()
        if d == 0:
            self._cand = 0
            self._cand_n = 0
            return
        if d == self._cand:
            self._cand_n += 1
        else:
            self._cand, self._cand_n = d, 1
        if self._cand_n < self.hold:
            return
        self._on_stable_press(it, d, raw)

    def _on_stable_press(self, it: Item, d: int, raw: bytes) -> None:
        rising = d & ~self._rest_int
        falling = d & self._rest_int
        if it.kind == "dpad":
            self._dpad[it.key] = {"rising": rising, "falling": falling, "raw": bytes(raw), "d": d}
            self._record(it, tr('got it'), ok=True)
            self.message = f"✓ {it.label}"
            self.peak_frames[f"DPAD_{it.key}"] = bytes(raw)
            self._to_release()
            return
        if _popcount(rising) == 1 and falling == 0:
            bit = rising.bit_length() - 1
            byte, b = bit // 8, bit % 8
            self.buttons[it.key] = (byte, b, it.target)
            self._claimed_int |= 1 << bit
            self._claimed_bytes.add(byte)
            self.peak_frames[it.key] = bytes(raw)
            self._record(it, tr('byte {0}, bit {1}', byte, b), ok=True)
            self.message = tr('✓ {0}: byte {1}, bit {2}', it.label, byte, b)
            self._to_release()
            return
        n = _popcount(d)
        self.message = (tr('{0} bits changed at once — several buttons were pressed or this is not an ordinary button. Release everything and press only: {1}', n, it.label))
        self._retry = True
        self._to_release(keep_step=True)

    def _to_release(self, keep_step: bool = False) -> None:
        self.phase = "release"
        self._release_n = 0
        self._keep_step = keep_step

    def _on_release(self, raw: bytes) -> None:
        it = self.current
        if it is not None and it.kind in ("stick", "trigger"):
            return
        d = self._persist()
        self._release_n = self._release_n + 1 if d == 0 else 0
        if self._release_n < self.hold:
            return
        if getattr(self, "_keep_step", False):
            self.phase = "step"
            self._cand = self._cand_n = 0
            return
        self._advance()

    # ---- D-pad -----------------------------------------------------------
    def _resolve_dpad_if_needed(self) -> None:
        if self._dpad and self.hat is None and not any(k.startswith("PAD_") for k in self.buttons):
            self._resolve_dpad()

    def _resolve_dpad(self) -> None:
        """Recognizes the D-pad from four recorded presses.

                Only what DIFFERS between the directions is useful: bytes changed identically in all four steps
                (an accelerometer shifting from the gamepad's tilt on press) say nothing about the D-pad and are ignored.
                a) one bit per direction: each step has its own bit that the other three lack;
                b) hat nibble: in ONE byte, the nibble values go around the circle up, up+2, up+4, up+6, and the neutral
                   (rest) state lies outside that range.
        """
        order = ("UP", "RIGHT", "DOWN", "LEFT")
        got = [self._dpad.get(k) for k in order]
        if any(g is None for g in got):
            if self._dpad:
                self.warnings.append(tr('D-pad: not all four directions were covered — not recognized.'))
            return
        
        rising = [g["rising"] for g in got]
        unique = [rising[i] & ~(rising[(i + 1) % 4] | rising[(i + 2) % 4] | rising[(i + 3) % 4]) for i in range(4)]
        if all(_popcount(u) == 1 for u in unique):
            bits = [u.bit_length() - 1 for u in unique]
            if len(set(bits)) == 4:
                for k, bit in zip(order, bits):
                    self.buttons[f"PAD_{k}"] = (bit // 8, bit % 8, k)
                    self._claimed_int |= 1 << bit
                    self._claimed_bytes.add(bit // 8)
                return
        
        for byte in range(1, self.L):
            if byte in {a["offset"] + k for a in self.axes.values() for k in range(a["width"])}:
                continue
            for shift in (0, 4):
                vals = [(g["raw"][byte] >> shift) & 0xF for g in got]
                up, right, down, left = vals
                neutral = (self.rest[byte] >> shift) & 0xF
                if (right, down, left) == (up + 2, up + 4, up + 6) and not up <= neutral <= up + 7:
                    self.hat = (byte, shift, up)
                    self._claimed_bytes.add(byte)
                    return
        self.warnings.append(tr('D-pad: the structure could not be recognized (neither bits nor a hat). Set it up manually or repeat the calibration.'))

    # ---- axes ----
    def _on_axis(self, it: Item, raw: bytes, now: float) -> None:
        self._ax_frames.append(bytes(raw))
        del self._ax_frames[:-MAX_AXIS_FRAMES]
        if now < self._ax_eval_at:
            return
        self._ax_eval_at = now + EVAL_PERIOD_S
        best = self._best_axis()
        self._ax_best = best
        if best is None:
            return
        if self._axis_complete(it, best):
            self._accept_axis(it, best)

    def _excluded_bytes(self) -> set:
        ex = set(self._claimed_bytes)
        for a in self.axes.values():
            ex.update(range(a["offset"], a["offset"] + a["width"]))
        ex.add(0)
        return ex

    @staticmethod
    def _value(f: bytes, off: int, fmt: str) -> int:
        if fmt == "u8":
            return f[off]
        v = (f[off] | f[off + 1] << 8) if fmt.endswith("le") else (f[off] << 8 | f[off + 1])
        if fmt.startswith("s") and v >= 0x8000:
            v -= 0x10000
        return v

    @staticmethod
    def _is_16bit_pair(low: List[int], high: List[int]) -> bool:
        """Whether two bytes form one 16-bit number, rather than two adjacent 8-bit axes/noise.

                A real 16-bit value: the low byte sweeps almost the whole range, and each time it "wraps" (255->0), the high
                byte changes by 1 in the opposite direction (a carry). Two independent axes (e.g. LX, LY on a DS4) have no such
                link: their signs are random. Without this check, adjacent u8 axes and gyro noise were taken for 16-bit values.
        """
        if max(low) - min(low) < 200:
            return False
        wraps = ok = 0
        for k in range(len(low) - 1):
            dl = low[k + 1] - low[k]
            if abs(dl) > 128:
                wraps += 1
                dh = (high[k + 1] - high[k] + 128) % 256 - 128          
                if dh != 0 and (dh > 0) == (dl < 0):
                    ok += 1
        return wraps >= 2 and ok >= 0.8 * wraps

    def _best_axis(self, force: bool = False) -> Optional[_Axis]:
        fr = self._ax_frames
        if len(fr) < 8:
            return None
        L = self.L
        ex = self._excluded_bytes()
        cols = [[f[i] for f in fr] for i in range(L)]
        act = [(max(c) - min(c)) - self.noise_span[i] if i not in ex else -1 for i, c in enumerate(cols)]
        it = self.current
        is_trig = it is not None and it.kind == "trigger"
        cands: List[Tuple[int, str]] = []
        for i in range(L):
            if act[i] >= 24 and i not in ex:
                cands.append((i, "u8"))
        for i in range(L - 1):
            if i in ex or i + 1 in ex:
                continue
            lo_b, hi_b = (i, i + 1)
            for a, b, suffix in ((lo_b, hi_b, "le"), (hi_b, lo_b, "be")):
                # "wide" byte — the low one (a); the high one (b) changes rarely
                if act[a] >= 24 and (max(cols[b]) - min(cols[b])) >= 1 and self._is_16bit_pair(cols[a], cols[b]):
                    for sg in ("u", "s"):
                        cands.append((min(i, i + 1), f"{sg}16{suffix}"))
        best: Optional[Tuple[float, int, _Axis]] = None
        seen = set()
        for off, fmt in cands:
            if (off, fmt) in seen:
                continue
            seen.add((off, fmt))
            width = 1 if fmt == "u8" else 2
            vals = [self._value(f, off, fmt) for f in fr]
            lo, hi = min(vals), max(vals)
            span = hi - lo
            if span <= 0:
                continue
            rest = self._value(self.rest, off, fmt)
            deltas = sorted(abs(vals[k + 1] - vals[k]) for k in range(len(vals) - 1))
            p90 = deltas[int(0.9 * (len(deltas) - 1))] if deltas else 0
            smooth = p90 / span
            if smooth > SMOOTH_P90 and not force:
                continue
            if not is_trig:
                tmin, tmax = (-32768, 32767) if fmt.startswith("s") else (0, 255 if width == 1 else 65535)
                mid = (tmin + tmax) / 2
                if abs(rest - mid) > 0.25 * (tmax - tmin):
                    continue
            thr_lo, thr_hi = rest - 1, rest + 1
            fs = 0
            for v in vals:
                if v <= rest - 0.2 * span and fs == 0:
                    fs = -1
                    break
                if v >= rest + 0.2 * span and fs == 0:
                    fs = 1
                    break
            a = _Axis(off, fmt, lo, hi, rest, smooth, fs, vals[-1], width)
            score = (span / ((65535 if width == 2 else 255))) if not is_trig else abs(max(hi - rest, rest - lo))
            pref = 1 if width == 2 else 0                  # the 16-bit smooth interpretation is more accurate
            key = (pref, score)
            if best is None or key > (best[1], best[0]):
                best = (score, pref, a)
        return best[2] if best else None

    def _settled(self, a: _Axis) -> bool:
        """An axis that has "settled": the last ~12 reports barely change. Without this, the low byte of a 16-bit value
        that passes through rest in the middle of a movement looks like a finished 8-bit axis.
        """
        tail = [self._value(f, a.offset, a.fmt) for f in self._ax_frames[-12:]]
        if len(tail) < 12:
            return False
        return max(tail) - min(tail) <= max(3, 0.03 * (a.hi - a.lo))

    def _axis_complete(self, it: Item, a: _Axis) -> bool:
        if not self._settled(a):
            return False
        if it.kind == "trigger":
            far = max(a.hi - a.rest, a.rest - a.lo)
            need = TRIGGER_MIN_MOVE[a.width]
            if far < need:
                return False
            return abs(a.cur - a.rest) <= 0.15 * far
        half = 32768.0 if a.width == 2 else STICK_MIN_HALF_U8
        reach = STICK_REACH * half
        if not (a.lo <= a.rest - reach and a.hi >= a.rest + reach):
            return False
        return abs(a.cur - a.rest) <= 0.2 * half

    def _accept_axis(self, it: Item, a: _Axis, forced: bool = False) -> None:
        d: dict = {"offset": a.offset, "fmt": a.fmt, "width": a.width}
        if it.kind == "trigger":
            rest, far_hi = a.rest, a.hi
            inverted = (a.rest - a.lo) > (a.hi - a.rest)
            ext = a.lo if inverted else a.hi
            m = 255
            for cap in (255, 1023, 4095, 65535):
                if max(ext, a.rest) <= cap:
                    m = cap
                    break
            d.update(raw_min=0, raw_max=m)
            if inverted:
                d["invert"] = True
            self.triggers["left" if it.key == "L2" else "right"] = it.key
        else:
            d["center"] = a.rest
            if it.expect_first_low and a.first_side == 1:
                d["invert"] = True
                self.warnings.append(tr('{0}: the direction was inverted according to your first movement.', it.key))
            side = "left" if it.key.startswith("L") else "right"
            self.sticks.setdefault(side, [None, None])[0 if it.key.endswith("X") else 1] = it.key
        self.axes[it.key] = d
        self.peak_frames[it.key] = self._ax_frames[-1] if self._ax_frames else b""
        self._record(it, tr('byte {0}, {1}', a.offset, a.fmt) + (tr(' (by the “Done” button)') if forced else ""), ok=True)
        self.message = tr('✓ {0}: byte {1}, format {2}', it.label, a.offset, a.fmt)
        self._advance()

    # ===================================================================
    
    # ===================================================================
    def build(self) -> dict:
        """Layout dictionary (the devices/*.json format). LayoutError if the result would not load."""
        if self.phase != "done":
            self._resolve_dpad_if_needed()
        d: dict = {
            "name": tr('{0} (calibration)', self.name or 'gamepad'),
            "match": {"vid": f"{self.vid:04x}", "pid": f"{self.pid:04x}"},
            "read_size": max(64, self.L),
            "priority": 10,
        }
        if self.interface is not None:
            d["match"]["interface"] = self.interface
        if self.L and not self._byte0_changed and self.rest:
            used = {b for b, _, _ in self.buttons.values()} | set().union(
                *[set(range(a["offset"], a["offset"] + a["width"])) for a in self.axes.values()] or [set()])
            if self.hat:
                used.add(self.hat[0])
            if 0 not in used:
                d["report_id"] = self.rest[0]
                if self.sync:
                    d["sync"] = {"byte": self.sync[0], "value": f"{self.sync[1]:02x}"}
        if self.handshake:
            d["handshake"] = self.handshake
        d["buttons"] = {k: {"byte": b, "bit": bit, "target": t} for k, (b, bit, t) in self.buttons.items()}
        if self.hat:
            byte, shift, first = self.hat
            d["hat"] = {"byte": byte, "shift": shift, "mask": 15, "first": first}
        fields: Dict[str, dict] = {}
        for k, a in self.axes.items():
            f: dict = {"offset": a["offset"]}
            if a["fmt"] != "u8" or True:
                f["fmt"] = a["fmt"]
            for opt in ("center", "raw_min", "raw_max", "invert"):
                if opt in a:
                    f[opt] = a[opt]
            fields[k] = f
        if fields:
            d["fields"] = fields
        sticks = {s: v for s, v in self.sticks.items() if v[0] in fields and v[1] in fields}
        if sticks:
            d["sticks"] = sticks
        trig = {s: k for s, k in self.triggers.items() if k in fields}
        if trig:
            d["triggers"] = trig
        d["_learned"] = {"method": "calibration", "notes": list(self.warnings),
                         "note": tr('The layout was built by the calibration wizard: check the buttons (gamepad-remapd testpage).')}
        return d

    def layout(self, layout_id: Optional[str] = None) -> DeviceLayout:
        lid = layout_id or (f"calibrated_{self.vid:04x}_{self.pid:04x}"
                            + (f"_if{self.interface}" if self.interface is not None else ""))
        return DeviceLayout.from_dict(self.build(), lid)

    def verify(self) -> List[str]:
        """Self-check on the saved frames: in the frame for button X, X must be the pressed one."""
        issues: List[str] = []
        try:
            lay = self.layout()
        except LayoutError as e:
            return [tr('the layout does not load: {0}', e)]
        pr = ReportParser(lay)
        rest_word, _ = pr.parse(self.rest) if self.rest and len(self.rest) >= pr.min_len else (0, ())
        named = sum(1 << i for i in pr.name_to_bit.values())     
        if rest_word & named:
            issues.append(tr('at rest the layout sees pressed buttons'))
        for key, frame in self.peak_frames.items():
            if key in lay.buttons and len(frame) >= pr.min_len:
                word, _ = pr.parse(frame)
                bit = pr.name_to_bit.get(key)
                if bit is not None and not (word >> bit) & 1:
                    issues.append(tr('button {0}: the press is not recognized in its frame', key))
        return issues
