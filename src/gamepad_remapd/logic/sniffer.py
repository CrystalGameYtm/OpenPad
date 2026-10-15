"""Hex-sniffing: автоматичне виявлення «прихованих» кнопок у сирих звітах.

Проблема: 2.4 ГГц ресивери (Flydigi, 8BitDo, GameSir ...) кладуть кнопки M1–M4,
C, Z та інші у довільні біти звіту, яких немає в стандартних XInput-описах.

Алгоритм (працює над потоком сирих звітів одного типу):

  1. BASELINE (≈2–3 с, геймпад не чіпають). Усі біти, що змінювались за цей час
     — шум: лічильники, таймстемпи, батарея, дрейф IMU та стіків.
     Усе, що не змінювалось, формує «спокійний» еталон `idle`.
  2. LIVE. Для кожного звіту рахуємо  active = (звіт ^ idle) & ~noise  —
     біти, що відхилились від спокою. Фронти active (0->1 / 1->0) = натискання
     та відпускання. Біт, що пройшов повний цикл, — кандидат у кнопки.
  3. Фільтр аналогових осей: байт, що в LIVE набрав багато різних значень, —
     вісь/лічильник, а не кнопки; його біти не пропонуються.
  4. Підказки: нібл із ≥4 значеннями з 0..8 (+15) схожий на hat-перемикач.
  5. LEARN: `learn("M1")` «озброює» захоплення — наступний стабільно утриманий
     біт (або біти) прив'язується до імені; результат експортується як
     фрагмент JSON секції "buttons" розкладки.

Нумерація: біт p = байт p//8, біт p%8 (як у розкладці). Байт 0 — це перший байт
того, що повернув read() (разом із Report ID, якщо він є).
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Dict, List, Optional, Set

from ..util import hex_str

ANALOG_DISTINCT_THRESHOLD = 24   # скільки різних значень байта робить його «віссю»
LEARN_STABLE_REPORTS = 3         # скільки звітів поспіль біт має триматись


class HexSniffer:
    def __init__(self, baseline_s: float = 2.5, numbered: bool = True,
                 report_id: Optional[int] = None):
        self.baseline_s = baseline_s
        self.numbered = numbered
        self.report_id = report_id
        self._lock = threading.Lock()   # feed() — потік читача, summary() — control-потік
        self.reset()

    # ---- стан --------------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._t0: Optional[float] = None
            self._phase = "baseline"
            self._key: Optional[tuple] = None
            self._streams = Counter()          # частоти типів звітів (ключ -> кількість)
            self._first = 0                    # перший звіт як int (еталон)
            self._noise = 0                    # маска шумових бітів
            self._idle = 0
            self._len = 0
            self._active_prev = 0
            self._press: Dict[int, int] = {}   # біт -> кількість натискань
            self._release: Dict[int, int] = {} # біт -> кількість відпускань
            self._values: Dict[int, Set[int]] = {}   # байт -> різні значення (LIVE)
            self._nibbles: Dict[tuple, Set[int]] = {}
            self._last_raw = b""
            self._changed_at: Dict[int, float] = {}  # байт -> час останньої зміни
            self._last_prev: Optional[bytes] = None
            self._n = 0
            # learn
            self._learn_name: Optional[str] = None
            self._learn_bits = 0
            self._learn_streak = 0
            self.learned: Dict[str, dict] = {}

    # ---- ГАРЯЧИЙ ВИКЛИК: з потоку читача, лише коли sniffing увімкнено -------
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
            if key != self._key:        # інший тип звіту (службові/статусні) — ігноруємо
                return
            self._live_step(raw, now)

    def _baseline_step(self, raw: bytes, key: tuple, now: float) -> None:
        # Під час baseline працюємо лише з найчастішим типом звіту.
        dominant = self._streams.most_common(1)[0][0]
        if key != dominant:
            return
        if self._key != dominant:       # домінанта змінилась -> перезапуск еталона
            self._key = dominant
            self._first = int.from_bytes(raw, "little")
            self._noise = 0
            self._len = len(raw)
            return
        v = int.from_bytes(raw, "little")
        self._noise |= v ^ self._first   # будь-який біт, що хоч раз змінився — шум
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
            for i, (a, b) in enumerate(zip(raw, prev)):  # для підсвітки змінених байтів у UI
                if a != b:
                    self._changed_at[i] = now
        self._last_prev = raw
        # статистика значень байтів/ніблів (для відсіву осей та пошуку hat)
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
        """Наступне стабільне натискання прив'язати до `name`."""
        with self._lock:
            self._learn_name, self._learn_bits, self._learn_streak = name, 0, 0

    def _learn_step(self, active: int) -> None:
        if self._learn_name is None:
            return
        analog = self._analog_bytes()
        # ігноруємо біти осей/лічильників
        for b in analog:
            active &= ~(0xFF << (b * 8))
        if active and active == self._learn_bits:
            self._learn_streak += 1
        else:
            self._learn_bits = active
            self._learn_streak = 1 if active else 0
        if self._learn_streak >= LEARN_STABLE_REPORTS:
            bits = [i for i in range(len(self._last_raw) * 8) if active >> i & 1]
            # Якщо одночасно змінились кілька бітів — це, імовірно, hat/кілька кнопок.
            self.learned[self._learn_name] = {
                "byte": bits[0] >> 3, "bit": bits[0] & 7, "also": [
                    {"byte": b >> 3, "bit": b & 7} for b in bits[1:]]}
            self._learn_name = None

    # ---- аналіз ---------------------------------------------------------------
    def _analog_bytes(self) -> Set[int]:
        return {i for i, vs in self._values.items() if len(vs) > ANALOG_DISTINCT_THRESHOLD}

    def summary(self) -> dict:
        """Знімок для UI/CLI (JSON-сумісний). Викликається з control-потоку."""
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
            # Кнопка, що натиснута просто зараз і ще не відпущена — теж показуємо.
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
                    hints.append(f"байт {i}, {'старший' if hi else 'молодший'} нібл: схоже на hat "
                                 f"(значення {sorted(vals)}); опишіть секцію \"hat\"")
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
        """Фрагмент секції "buttons" розкладки з усього, що вивчено через learn()."""
        def build() -> dict:
            return {n: {"byte": v["byte"], "bit": v["bit"], "target": None}
                    for n, v in self.learned.items()}
        if _locked:
            return build()
        with self._lock:
            return build()
