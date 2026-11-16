"""HOT PATH: raw HID report -> parsing -> mapping -> virtual gamepad.

A single call to `Pipeline.process(raw)` runs the whole chain synchronously in the
reader thread. No asyncio transitions, queues, cross-thread copies or large
allocations: the software processing latency is the time of the call itself
(measured with a built-in histogram, see `stats()`).

Button mapping works on bit masks with an XOR diff:
    changed = src ^ src_prev
Only the bits that changed are processed (usually 0–2 per report), so the cost does not
depend on the number of buttons. Each target keeps a reference counter: if two physical
buttons map to one virtual button, it is released only when both are released. The number
of simultaneously held buttons is unlimited (no 6KRO/NKRO arrays — just a bit mask of any length).
"""
from __future__ import annotations

from math import sqrt  # noqa: F401  (kept for profilers; sqrt is used by StickProc)
from time import perf_counter_ns
from typing import List, Optional

from ..output.base import OutputBackend
from .curves import LUT_N  # noqa: F401
from .profile import CompiledProfile

HYST = 8  
HIST_BUCKETS = 2048  # 1 µs per bucket; anything longer than 2 ms goes into the last one


class Pipeline:
    __slots__ = ("out", "cp", "_seen", "ref", "src_prev", "last", "full_l", "full_r", "st_l", "st_r",
                 "count", "hist", "max_ns", "total_ns", "sniffer", "last_raw", "dropped")

    def __init__(self, out: OutputBackend):
        self.out = out
        self.cp: Optional[CompiledProfile] = None   
        self._seen: Optional[CompiledProfile] = None
        self.ref: List[int] = []                    
        self.src_prev = 0                           
        self.last = [0, 0, 0, 0, 0, 0]              
        self.st_l = [False, False]                  
        self.st_r = [False, False]
        self.full_l = self.full_r = 0               # is the "digital" LT/RT held?
        self.count = 0
        self.hist = [0] * HIST_BUCKETS
        self.max_ns = 0
        self.total_ns = 0
        self.sniffer = None                         # HexSniffer | None
        self.last_raw = b""
        self.dropped = 0                            

    # ---- control (called from the reader thread) ----
    def _rebind(self, cp: CompiledProfile) -> None:
        """Switch to a new profile: resets the state and re-applies the held buttons,
        so the new map takes effect without a release/press.
        """
        self.out.release_all()
        self.ref = [0] * (cp.n_targets or cp.n_buttons + 2)
        self.src_prev = 0       
        self.last = [0, 0, 0, 0, 0, 0]
        self.full_l = self.full_r = 0
        self.st_l = [False, False]
        self.st_r = [False, False]
        self._seen = cp

    def reset_stats(self) -> None:
        self.count = 0
        self.hist = [0] * HIST_BUCKETS
        self.max_ns = 0
        self.total_ns = 0
        self.dropped = 0

    def stats(self) -> dict:
        """Processing-time statistics of one report (µs), from a 1 µs histogram."""
        n = self.count
        if not n:
            return {"count": 0, "dropped": self.dropped}

        def pct(p: float) -> int:
            target, acc = n * p, 0
            for i, c in enumerate(self.hist):
                acc += c
                if acc >= target:
                    return i
            return HIST_BUCKETS - 1
        return {"count": n, "dropped": self.dropped,
                "mean_us": round(self.total_ns / n / 1000, 2),
                "p50_us": pct(0.50), "p99_us": pct(0.99), "p999_us": pct(0.999),
                "max_us": round(self.max_ns / 1000, 1)}

    def _hold(self, targets, down: bool) -> None:
        """Press/release targets using reference counters (buttons, LT/RT, keys)."""
        ref, out, cp = self.ref, self.out, self.cp
        nb = cp.n_buttons
        if down:
            for t in targets:
                r = ref[t]
                ref[t] = r + 1
                if r == 0:                          
                    if t < nb:
                        out.set_button(t, True)
                    elif t == nb:
                        self.full_l = cp.press_l or 1
                    elif t == nb + 1:
                        self.full_r = cp.press_r or 1
                    else:                           
                        out.set_key(cp.key_codes[t - nb - 2], True)
        else:
            for t in targets:
                r = ref[t] - 1
                ref[t] = r
                if r == 0:                          
                    if t < nb:
                        out.set_button(t, False)
                    elif t == nb:
                        self.full_l = 0
                    elif t == nb + 1:
                        self.full_r = 0
                    else:
                        out.set_key(cp.key_codes[t - nb - 2], False)

    def _stages(self, level: int, stages, on: list) -> None:
        """Trigger stages: a threshold with hysteresis; a state change presses/releases the targets."""
        for k, (thr, ids) in enumerate(stages):
            if on[k]:
                if level < thr - HYST:
                    on[k] = False
                    self._hold(ids, False)
            elif level >= thr:
                on[k] = True
                self._hold(ids, True)

    # ---- HOT PATH ----
    def process(self, raw: bytes) -> None:
        t0 = perf_counter_ns()
        sn = self.sniffer
        if sn is not None:          
            sn.feed(raw)
        cp = self.cp
        if cp is None:              
            return
        if cp is not self._seen:    # the control thread swapped the profile -> hot swap
            self._rebind(cp)
        parser = cp.parser
        
        if len(raw) < parser.min_len or (parser.rid is not None and raw[0] != parser.rid) \
                or (parser.sync is not None and raw[parser.sync[0]] != parser.sync[1]):
            self.dropped += 1
            return
        self.last_raw = raw

        # 1) PARSING: two C calls -> button mask + tuple of numeric fields.
        src, vals = parser.parse(raw)
        out = self.out
        ref = self.ref

        # 2) BUTTONS: XOR diff against the previous state; only the changed bits are touched.
        changed = src ^ self.src_prev
        if changed:
            self.src_prev = src
            table = cp.bit_targets
            nb = cp.n_buttons
            while changed:
                low = changed & -changed            
                changed ^= low
                targets = table[low.bit_length() - 1]
                if targets is None:
                    continue
                self._hold(targets, bool(src & low))

        # 3) AXES: sticks (deadzone + curve), gyro, triggers.
        left, right = cp.left, cp.right
        lx, ly = left.apply(vals) if left is not None else (0.0, 0.0)
        rx, ry = right.apply(vals) if right is not None else (0.0, 0.0)
        gyro = cp.gyro
        if gyro is not None and gyro.to_mouse:
            mx, my = gyro.mouse(vals, src, t0)
            if mx or my:
                out.move_mouse(mx, my)
        elif gyro is not None:
            gx, gy = gyro.apply(vals, src)
            if gx or gy:
                if gyro.to_right:
                    rx += gx
                    ry += gy
                else:
                    lx += gx
                    ly += gy
        if cp.swap_sticks:
            lx, ly, rx, ry = rx, ry, lx, ly
        tl, tr = cp.trig_l, cp.trig_r
        lt = rt = 0
        if tl is not None:
            lt = tl.apply(vals)                     
            if cp.stages_l:
                self._stages(lt, cp.stages_l, self.st_l)
            lt = tl.out[lt]                         
        if tr is not None:
            rt = tr.apply(vals)
            if cp.stages_r:
                self._stages(rt, cp.stages_r, self.st_r)
            rt = tr.out[rt]
        if cp.swap_triggers:
            lt, rt = rt, lt
        if self.full_l and lt < self.full_l:        
            lt = self.full_l
        if self.full_r and rt < self.full_r:
            rt = self.full_r

        # float [-1,1] -> clamped int16 (gyro + stick sum may exceed the range)
        ax = (int(_clamp(lx) * 32767.0), int(_clamp(ly) * 32767.0),
              int(_clamp(rx) * 32767.0), int(_clamp(ry) * 32767.0), lt, rt)
        last = self.last
        ids = cp.axis_ids
        for k in range(6):                          
            v = ax[k]
            if v != last[k]:
                last[k] = v
                out.set_axis(ids[k], v)

        # 4) OUTPUT: one system call per report (all changes + SYN_REPORT).
        out.flush()

        # 5) STATS: processing latency statistics.
        dt = perf_counter_ns() - t0
        self.count += 1
        self.total_ns += dt
        if dt > self.max_ns:
            self.max_ns = dt
        b = dt // 1000
        self.hist[b if b < HIST_BUCKETS else HIST_BUCKETS - 1] += 1


def _clamp(v: float) -> float:
    return -1.0 if v < -1.0 else 1.0 if v > 1.0 else v
