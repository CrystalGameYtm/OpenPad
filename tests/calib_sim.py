"""Симулятор пристрою + «віртуальний користувач» для тестів майстра калібрування.

Пристрій описується словником-істиною (де лежать кнопки, осі, hat), з нього будуються сирі звіти;
`run_wizard` проходить усі кроки Calibrator так, як це зробила б людина, а тест порівнює результат
з істиною. Так майстер перевіряється на форматах без жодного реального пристрою.
"""
from __future__ import annotations

import random
from typing import Dict, Optional

from gamepad_remapd.logic.calibrate import Calibrator

DT = 0.004            # 250 Гц
PRESS_FRAMES = 20     # ~80 мс: реалістична тривалість дотику (швидше за ~50 мс люди кнопки майже не тиснуть)


class SimPad:
    def __init__(self, spec: dict, seed: int = 1):
        self.s = spec
        self.rng = random.Random(seed)
        self.pressed: set = set()
        self.hat: Optional[str] = None
        self.axis: Dict[str, float] = {}        # ім'я осі -> фізичне положення: -1..1 (для тригера 0..1)
        self.seq = 0
        self.tilt = 0

    # --- кодування значення осі ---
    @staticmethod
    def _put(b: bytearray, off: int, fmt: str, v: int) -> None:
        if fmt == "u8":
            b[off] = v & 0xFF
            return
        v &= 0xFFFF
        if fmt.endswith("le"):
            b[off], b[off + 1] = v & 0xFF, v >> 8
        else:
            b[off], b[off + 1] = v >> 8, v & 0xFF

    def frame(self) -> bytes:
        s = self.s
        b = bytearray(s["len"])
        for off, val in s.get("const", {}).items():
            b[off] = val
        for name, (byte, bit) in s["buttons"].items():
            if name in self.pressed:
                b[byte] |= 1 << bit
        hat = s.get("hat")
        if hat:
            byte, shift, order = hat["byte"], hat["shift"], ("UP", "RIGHT", "DOWN", "LEFT")
            v = 8 if self.hat is None else self.hat_value(order)
            b[byte] = (b[byte] & ~(0xF << shift)) | (v << shift)
        moving = any(abs(v) > 0.02 for k, v in self.axis.items() if not s["axes"][k].get("trigger"))
        for name, a in s["axes"].items():
            p = self.axis.get(name, 0.0)
            if a.get("trigger"):
                raw = round(p * a["max"])
                raw = a["max"] - raw if a.get("inverted") else raw
            else:
                sgn = -1 if a.get("inverted") else 1
                raw = round(a["center"] + sgn * p * a["half"])
                if moving and s.get("crosstalk") and abs(p) < 0.02:     # перехресні наводки на нерухому вісь
                    raw += self.rng.randint(-s["crosstalk"], s["crosstalk"])
            self._put(b, a["offset"], a["fmt"], raw)
        for off in s.get("noise", ()):            # «гіроскоп»: шум у спокої
            b[off] = self.rng.randrange(256)
        if (self.pressed or self.hat is not None) and s.get("hand"):   # рука стискає корпус: акселерометр/гіро «співають»
            for off in s["hand"]:
                b[off] = self.rng.randrange(256)
        if (self.pressed or self.hat is not None) and s.get("drift"):
            # натискання нахиляє геймпад: акселерометр плавно «їде», біти тримаються кілька кадрів
            self.tilt = min(self.tilt + 1, 60)
            for off in s["drift"]:
                b[off] = 0x21 + self.tilt // s.get("drift_step", 2)
        else:
            self.tilt = 0
        if moving:                                # рух стіка трусить геймпад: гіро/акселерометр «співають» ширше
            for off in s.get("shake", ()):
                b[off] = self.rng.randrange(256)
        if s.get("counter") is not None:           # (байт, зсув): лічильник у старших бітах, молодші — кнопки
            byte, shift = s["counter"]
            self.seq += 1
            b[byte] |= ((self.seq & (0xFF >> shift)) << shift) & 0xFF
        return bytes(b)

    def hat_value(self, order) -> int:
        return {"UP": 0, "RIGHT": 2, "DOWN": 4, "LEFT": 6}[self.hat]


def script(spec: dict, get_cal, *, absent=(), baseline_frames: int = 900, seed: int = 1):
    """Генератор кадрів: поводиться як людина, що проходить кроки майстра. Дивиться на поточний крок
    через `get_cal()`, сам натискає «Пропустити»/«Готово» (cal.skip / cal.finish_step)."""
    pad = SimPad(spec, seed)

    def ticks(n):
        for _ in range(n):
            yield pad.frame()

    yield from ticks(baseline_frames)
    guard = 0
    cal = get_cal()
    while not cal.finished and guard < 400:
        guard += 1
        it = cal.current
        if it is None:                      # ще baseline
            yield from ticks(50)
            continue
        key = it.key
        if key in absent:
            cal.skip()
            continue
        if it.kind in ("button", "dpad"):
            name = key if it.kind == "button" else (None if spec.get("hat") else f"PAD_{key}")
            if it.kind == "button" and key not in spec["buttons"] or \
                    it.kind == "dpad" and not spec.get("hat") and name not in spec["buttons"]:
                cal.skip()
                continue
            if it.kind == "dpad" and spec.get("hat"):
                pad.hat = key
            else:
                pad.pressed = {name}
            yield from ticks(PRESS_FRAMES)
            pad.pressed, pad.hat = set(), None
            yield from ticks(PRESS_FRAMES)
        elif it.kind == "stick":
            a = spec["axes"].get(key)
            if a is None:
                cal.skip()
                continue
            leg = spec.get("leg", 60)
            for target in (-1.0, 1.0, 0.0):          # спершу вліво/вгору, потім вправо/вниз, у центр
                start = pad.axis.get(key, 0.0)
                for k in range(1, leg + 1):
                    pad.axis[key] = start + (target - start) * k / leg
                    yield pad.frame()
            yield from ticks(20)
            if not cal.finished and cal.current is it:
                cal.finish_step()
        elif it.kind == "trigger":
            a = spec["axes"].get(key)
            if a is None:
                cal.skip()
                continue
            for target in (1.0, 0.0):
                start = pad.axis.get(key, 0.0)
                for k in range(1, 41):
                    pad.axis[key] = start + (target - start) * k / 40
                    yield pad.frame()
            yield from ticks(20)
            if not cal.finished and cal.current is it:
                cal.finish_step()


def run_wizard(spec: dict, *, absent=(), baseline_frames: int = 900, seed: int = 1,
               cal: Optional[Calibrator] = None) -> Calibrator:
    """Проходить усі кроки напряму (з модельним часом 250 Гц). `absent` — ключі, яких на пристрої немає."""
    cal = cal or Calibrator(0x1234, 0x5678, "Sim pad")
    t = 0.0
    for frame in script(spec, lambda: cal, absent=absent, baseline_frames=baseline_frames, seed=seed):
        t += DT
        cal.feed(frame, t)
    return cal


# ---------------------------------------------------------------- набір відомих форматів ----
XBOX360_USB = {      # xusb: [0]=0, [1]=0x14, [2..3] кнопки, [4],[5] тригери u8, [6..13] стіки s16le
    "len": 20,
    "const": {1: 0x14},
    "buttons": {
        "PAD_UP": (2, 0), "PAD_DOWN": (2, 1), "PAD_LEFT": (2, 2), "PAD_RIGHT": (2, 3),
        "START": (2, 4), "BACK": (2, 5), "LS": (2, 6), "RS": (2, 7),
        "LB": (3, 0), "RB": (3, 1), "GUIDE": (3, 2), "A": (3, 4), "B": (3, 5), "X": (3, 6), "Y": (3, 7)},
    "axes": {
        "L2": {"offset": 4, "fmt": "u8", "trigger": True, "max": 255},
        "R2": {"offset": 5, "fmt": "u8", "trigger": True, "max": 255},
        "LX": {"offset": 6, "fmt": "s16le", "center": 0, "half": 32767},
        "LY": {"offset": 8, "fmt": "s16le", "center": 0, "half": 32767, "inverted": True},   # Y вгору = +
        "RX": {"offset": 10, "fmt": "s16le", "center": 0, "half": 32767},
        "RY": {"offset": 12, "fmt": "s16le", "center": 0, "half": 32767, "inverted": True}},
}

DS4_USB = {
    "len": 64,
    "const": {0: 1},
    "buttons": {"X": (5, 4), "A": (5, 5), "B": (5, 6), "Y": (5, 7), "LB": (6, 0), "RB": (6, 1),
                "BACK": (6, 4), "START": (6, 5), "LS": (6, 6), "RS": (6, 7), "GUIDE": (7, 0)},
    "hat": {"byte": 5, "shift": 0},
    "axes": {
        "LX": {"offset": 1, "fmt": "u8", "center": 128, "half": 127},
        "LY": {"offset": 2, "fmt": "u8", "center": 128, "half": 127},
        "RX": {"offset": 3, "fmt": "u8", "center": 128, "half": 127},
        "RY": {"offset": 4, "fmt": "u8", "center": 128, "half": 127},
        "L2": {"offset": 8, "fmt": "u8", "trigger": True, "max": 255},
        "R2": {"offset": 9, "fmt": "u8", "trigger": True, "max": 255}},
    "noise": [13, 14, 15, 16, 17, 18],           # гіроскоп
    "hand": [19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30],   # на столі тихо, у руці при натисканні «співає»
    "shake": [34, 35, 36, 37, 38, 39],           # область тачпада/гіро-кадрів: «співає» при русі стіка
    "drift": [19, 20, 21, 22], "drift_step": 7,                   # акселерометр: плавний дрейф при натисканні (у спокої стабільний)
    "crosstalk": 3,                              # наводки на сусідні осі, як у справжніх стіках
    "counter": (7, 2),                           # як у справжнього DS4: лічильник у бітах 2..7 байта 7
}

FLYDIGI_V1 = {
    "len": 32,
    "const": {0: 4, 1: 0xFE},
    "buttons": {"A": (9, 4), "B": (9, 5), "BACK": (9, 6), "X": (9, 7), "Y": (10, 0), "START": (10, 1),
                "LB": (10, 2), "RB": (10, 3), "LS": (10, 6), "RS": (10, 7), "GUIDE": (8, 3),
                "M1": (7, 2), "M2": (7, 3), "M3": (7, 4), "M4": (7, 5), "C": (7, 0), "Z": (7, 1),
                "PAD_UP": (9, 0), "PAD_RIGHT": (9, 1), "PAD_DOWN": (9, 2), "PAD_LEFT": (9, 3)},
    "axes": {
        "LX": {"offset": 17, "fmt": "u8", "center": 127, "half": 127},
        "LY": {"offset": 19, "fmt": "u8", "center": 127, "half": 127},
        "RX": {"offset": 21, "fmt": "u8", "center": 127, "half": 127},
        "RY": {"offset": 22, "fmt": "u8", "center": 127, "half": 127},
        "L2": {"offset": 23, "fmt": "u8", "trigger": True, "max": 255},
        "R2": {"offset": 24, "fmt": "u8", "trigger": True, "max": 255}},
}

# безіменний «китайський» пристрій: 16-бітні BE осі, беззнакові, 10-бітні тригери, hat у старшому ніббле
GENERIC_BE16 = {
    "len": 24,
    "const": {0: 0x81},
    "buttons": {"A": (1, 0), "B": (1, 1), "X": (1, 2), "Y": (1, 3), "LB": (1, 4), "RB": (1, 5),
                "BACK": (2, 0), "START": (2, 1), "LS": (2, 2), "RS": (2, 3), "GUIDE": (2, 4),
                "ML": (2, 5), "MR": (2, 6)},
    "hat": {"byte": 3, "shift": 4},
    "axes": {
        "LX": {"offset": 4, "fmt": "u16be", "center": 32768, "half": 32767},
        "LY": {"offset": 6, "fmt": "u16be", "center": 32768, "half": 32767},
        "RX": {"offset": 8, "fmt": "u16be", "center": 32768, "half": 32767},
        "RY": {"offset": 10, "fmt": "u16be", "center": 32768, "half": 32767},
        "L2": {"offset": 12, "fmt": "u16le", "trigger": True, "max": 1023},
        "R2": {"offset": 14, "fmt": "u16le", "trigger": True, "max": 1023}},
    "noise": [20, 21],
    "leg": 600,                                  # 16-бітна вісь: рух повільний, як у людини
}

ALL = {"xbox360": XBOX360_USB, "ds4_usb": DS4_USB, "flydigi_v1": FLYDIGI_V1, "generic_be16": GENERIC_BE16}
