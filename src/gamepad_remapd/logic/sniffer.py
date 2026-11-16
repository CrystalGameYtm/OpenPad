"""Hex sniffing: automatic detection of "hidden" buttons in raw reports.

Problem: 2.4 GHz receivers (Flydigi, 8BitDo, GameSir ...) put the M1–M4,
C, Z and other buttons into arbitrary report bits that are absent from the standard XInput descriptions.

Algorithm (works on the stream of raw reports of one type):

  1. BASELINE (≈2–3 s, the gamepad is not touched). All bits that changed during this time
     are noise: counters, timestamps, battery, IMU and stick drift.
     Everything that did not change forms the "calm" reference `idle`.
  2. LIVE. For each report we compute  active = (report ^ idle) & ~noise  —
     the bits that deviated from rest. The edges of active (0->1 / 1->0) are presses and releases.
     A bit that completed a full cycle is a button candidate.
  3. Analog axis filter: a byte that took many different values during LIVE is an axis/counter,
     not buttons; its bits are not proposed.
  4. Hints: a nibble with ≥4 values from 0..8 (+15) looks like a hat switch.
  5. LEARN: `learn("M1")` "arms" the capture — the next steadily held bit (or bits) is bound to the name;
     the result is exported as a fragment of the layout's JSON "buttons" section.

Numbering: bit p = byte p//8, bit p%8 (as in the layout). Byte 0 is the first byte of what read()
returned (including the Report ID, if there is one).
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Dict, List, Optional, Set

from ..util import hex_str
from ..i18n import tr

ANALOG_DISTINCT_THRESHOLD = 24   
LEARN_STABLE_REPORTS = 3         


class HexSniffer:
    def __init__(self, baseline_s: float = 2.5, numbered: bool = True,
                 report_id: Optional[int] = None):
        self.baseline_s = baseline_s
        self.numbered = numbered
        self.report_id = report_id
        self._lock = threading.Lock()   # feed() — the reader thread, summary() — the control thread
        self.reset()

    # ---- state ----
    def reset(self) -> None:
        with self._lock:
            self._t0: Optional[float] = None
            self._phase = "baseline"
            self._key: Optional[tuple] = None
            self._streams = Counter()          
            self._first = 0                    
            self._noise = 0                    
            self._idle = 0
            self._len = 0
            self._active_prev = 0
            self._press: Dict[int, int] = {}   
            self._release: Dict[int, int] = {} 
            self._values: Dict[int, Set[int]] = {}   
            self._nibbles: Dict[tuple, Set[int]] = {}
            self._last_raw = b""
            self._changed_at: Dict[int, float] = {}  
            self._last_prev: Optional[bytes] = None
            self._n = 0
            # learn
            self._learn_name: Optional[str] = None
            self._learn_bits = 0
            self._learn_streak = 0
            self.learned: Dict[str, dict] = {}

    # ---- HOT CALL: from the reader thread, only when sniffing is enabled ----
    def feed(self, raw: bytes) -> None:
        if not raw:
            return
        if self.report_id is not None and (not raw or raw[0] != self.report_id):
            return
        key = (len(raw), raw[0] if self.numbered else None)
        now = time.monotonic()
        with self._lock:
            if self._t0 is None:
                self._t0 = now
            if self._phase == "baseline":
                self._streams[key] += 1
                self._baseline_step(raw, key, now)
                return
            if key != self._key:        
                return
            self._live_step(raw, now)

    def _baseline_step(self, raw: bytes, key: tuple, now: float) -> None:
        
        dominant = self._streams.most_common(1)[0][0]
        if key != dominant:
            return
        if self._key != dominant:       
            self._key = dominant
            self._first = int.from_bytes(raw, "little")
            self._noise = 0
            self._len = len(raw)
            return
        v = int.from_bytes(raw, "little")
        self._noise |= v ^ self._first   
        self._last_raw = raw
        if now - (self._t0 or now) >= self.baseline_s:
            self._idle = self._first & ~self._noise
            self._phase = "live"

    def _live_step(self, raw: bytes, now: float) -> None:
        self._n += 1
        self._last_raw = raw
        v = int.from_bytes(raw, "little")
        prev = self._last_prev
        if prev is not None and len(prev) == len(raw):
            for i, (a, b) in enumerate(zip(raw, prev)):  
                if a != b:
                    self._changed_at[i] = now
        self._last_prev = raw
        
        for i, b in enumerate(raw):
            vs = self._values.setdefault(i, set())
            if len(vs) <= ANALOG_DISTINCT_THRESHOLD + 1:
                vs.add(b)
            self._nibbles.setdefault((i, 0), set()).add(b & 0x0F)
            self._nibbles.setdefault((i, 1), set()).add(b >> 4)
        active = (v ^ self._idle) & ~self._noise
        prev_active = self._active_prev
        if active != prev_active:
            rising, falling = active & ~prev_active, prev_active & ~active
            for mask, table in ((rising, self._press), (falling, self._release)):
                while mask:
                    low = mask & -mask
                    bit = low.bit_length() - 1
                    table[bit] = table.get(bit, 0) + 1
                    mask ^= low
            self._active_prev = active
        self._learn_step(active)

    # ---- guided learn ---------------------------------------------------------
    def learn(self, name: str) -> None:
        """Binds the next stable press to `name`."""
        with self._lock:
            self._learn_name, self._learn_bits, self._learn_streak = name, 0, 0

    def _learn_step(self, active: int) -> None:
        if self._learn_name is None:
            return
        analog = self._analog_bytes()
        
        for b in analog:
            active &= ~(0xFF << (b * 8))
        if active and active == self._learn_bits:
            self._learn_streak += 1
        else:
            self._learn_bits = active
            self._learn_streak = 1 if active else 0
        if self._learn_streak >= LEARN_STABLE_REPORTS:
            bits = [i for i in range(len(self._last_raw) * 8) if active >> i & 1]
            
            self.learned[self._learn_name] = {
                "byte": bits[0] >> 3, "bit": bits[0] & 7, "also": [
                    {"byte": b >> 3, "bit": b & 7} for b in bits[1:]]}
            self._learn_name = None

    # ---- analysis ----
    def _analog_bytes(self) -> Set[int]:
        return {i for i, vs in self._values.items() if len(vs) > ANALOG_DISTINCT_THRESHOLD}

    def summary(self) -> dict:
        """A snapshot for the UI/CLI (JSON-compatible). Called from the control thread."""
        with self._lock:
            now = time.monotonic()
            progress = 1.0
            if self._phase == "baseline" and self._t0 is not None:
                progress = min(1.0, (now - self._t0) / self.baseline_s)
            elif self._phase == "baseline":
                progress = 0.0
            analog = self._analog_bytes()
            noise_bytes = sorted({i for i in range(self._len)
                                  if (self._noise >> (i * 8)) & 0xFF})
            cands = []
            for bit in sorted(set(self._press) & set(self._release)):
                byte = bit >> 3
                if byte in analog:
                    continue
                cands.append({"byte": byte, "bit": bit & 7, "mask": f"0x{1 << (bit & 7):02x}",
                              "presses": self._press[bit],
                              "down": bool((self._active_prev >> bit) & 1)})
            
            for bit in range(self._len * 8):
                if (self._active_prev >> bit) & 1 and (bit >> 3) not in analog \
                        and not any(c["byte"] == bit >> 3 and c["bit"] == (bit & 7) for c in cands):
                    cands.append({"byte": bit >> 3, "bit": bit & 7, "mask": f"0x{1 << (bit & 7):02x}",
                                  "presses": self._press.get(bit, 0), "down": True})
            hints = []
            for (i, hi), vals in sorted(self._nibbles.items()):
                if i in analog:
                    continue
                if len(vals) >= 4 and vals <= set(range(0, 9)) | {15} and (8 in vals or 15 in vals):
                    hints.append(tr('byte {0}, {1} nibble: looks like a hat (values {2}); describe a "hat" section', i, tr('high') if hi else tr('low'), sorted(vals)))
            return {
                "phase": self._phase, "progress": round(progress, 2),
                "reports": self._n, "length": self._len,
                "hex": hex_str(self._last_raw),
                "recent_changed": sorted(i for i, t in self._changed_at.items() if now - t < 0.6),
                "noise_bytes": noise_bytes, "analog_bytes": sorted(analog),
                "candidates": sorted(cands, key=lambda c: (c["byte"], c["bit"])),
                "hints": hints, "learning": self._learn_name,
                "learned": dict(self.learned),
                "layout_snippet": self.layout_snippet(_locked=True),
            }

    def layout_snippet(self, _locked: bool = False) -> dict:
        """A fragment of the layout's "buttons" section built from everything learned via learn()."""
        def build() -> dict:
            return {n: {"byte": v["byte"], "bit": v["bit"], "target": None}
                    for n, v in self.learned.items()}
        if _locked:
            return build()
        with self._lock:
            return build()
