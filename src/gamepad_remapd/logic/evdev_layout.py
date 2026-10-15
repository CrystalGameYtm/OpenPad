"""Розкладка для evdev-геймпадів (XInput-режим, xpad, hid-playstation, hid-nintendo ...).

Ядро Linux уже переклало такий пристрій у стандартні коди подій (BTN_SOUTH, ABS_X ...), тож тут
НЕМАЄ евристики за порядком кнопок, як у HID-дескрипторах: BTN_SOUTH — це завжди «A/хрестик»,
BTN_TL — LB тощо. Щоб решта конвеєра (ReportParser, профілі, UI) працювала без змін, стан
пристрою пакується у «синтетичний звіт» фіксованого формату:

    байт 0              Report ID = 0xE5
    байти 1..N          бітова маска кнопок (за зростанням коду evdev)
    наступний байт      hat D-pad (ABS_HAT0X/Y): 0..7 по колу від «вгору», 8 = нейтраль
    далі по 2 байти     осі, int16 little-endian (за зростанням коду ABS_*)

`spec_from_caps()` — детермінована функція можливостей пристрою; і розкладка, і пакувальник
у input/evdev_linux.py беруть її результат, тож формат завжди збігається.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .layout import DeviceLayout

REPORT_ID = 0xE5
S16_MIN, S16_MAX = -32768, 32767

# --- коди evdev (linux/input-event-codes.h) ---
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

# код кнопки -> (ім'я джерела в розкладці, вихідна ціль). Ім'я DPAD_* зарезервоване для hat,
# тому цифровий D-pad називається PAD_*.
BUTTONS: Dict[int, Tuple[str, Optional[str]]] = {
    0x130: ("SOUTH", "A"), 0x131: ("EAST", "B"), 0x132: ("C", "C"),
    0x133: ("NORTH", "X"), 0x134: ("WEST", "Y"), 0x135: ("Z", "Z"),
    0x136: ("TL", "LB"), 0x137: ("TR", "RB"), 0x138: ("TL2", "LT"), 0x139: ("TR2", "RT"),
    0x13A: ("SELECT", "BACK"), 0x13B: ("START", "START"), 0x13C: ("MODE", "GUIDE"),
    0x13D: ("THUMBL", "LS"), 0x13E: ("THUMBR", "RS"),
    0x220: ("PAD_UP", "UP"), 0x221: ("PAD_DOWN", "DOWN"),
    0x222: ("PAD_LEFT", "LEFT"), 0x223: ("PAD_RIGHT", "RIGHT"),
}
_HAPPY_BASE = 0x2C0                       # BTN_TRIGGER_HAPPY1..; веслá/задні кнопки -> M1..M6, C, Z
_HAPPY_TARGETS = ("M1", "M2", "M3", "M4", "M5", "M6")
GAMEPAD_KEY_RANGES = ((0x120, 0x140), (0x220, 0x224))


@dataclass(frozen=True)
class EvdevSpec:
    keys: Tuple[int, ...]                       # коди кнопок за зростанням
    hat: Optional[Tuple[int, int]]              # (ABS_HAT0X, ABS_HAT0Y) або None
    hat_byte: int
    axes: Tuple[Tuple[int, int, int, int], ...]  # (код ABS, зсув у звіті, min, max)
    size: int                                   # довжина синтетичного звіту


def is_gamepad(keys, abs_codes) -> bool:
    """Геймпад/джойстик: має кнопки діапазону BTN_JOYSTICK/GAMEPAD (або D-pad-кнопки) і осі.
    Миші (BTN_LEFT), тачпади й планшети (BTN_TOUCH/STYLUS) сюди не потрапляють."""
    has_btn = any(lo <= k < hi for k in keys for lo, hi in GAMEPAD_KEY_RANGES)
    has_axes = (ABS_X in abs_codes and ABS_Y in abs_codes) or ABS_HAT0X in abs_codes
    return has_btn and has_axes


def spec_from_caps(keys, abs_info: Dict[int, Tuple[int, int]]) -> EvdevSpec:
    """keys — набір кодів кнопок; abs_info — {код ABS: (min, max)}."""
    ks = tuple(sorted(set(keys)))
    nbtn = (len(ks) + 7) // 8
    pos = 1 + nbtn
    hat = (ABS_HAT0X, ABS_HAT0Y) if ABS_HAT0X in abs_info and ABS_HAT0Y in abs_info else None
    hat_byte = pos
    if hat:
        pos += 1
    axes: List[Tuple[int, int, int, int]] = []
    for code in sorted(abs_info):
        if hat and code in hat:
            continue
        lo, hi = abs_info[code]
        lo, hi = max(S16_MIN, int(lo)), min(S16_MAX, int(hi))
        if hi <= lo:
            continue                                  # вироджена вісь (min==max) — нічого віддавати
        axes.append((code, pos, lo, hi))
        pos += 2
    return EvdevSpec(ks, hat, hat_byte, tuple(axes), pos)


def _key_name(code: int) -> Tuple[str, Optional[str]]:
    if code in BUTTONS:
        return BUTTONS[code]
    n = code - _HAPPY_BASE
    if 0 <= n < 64:
        return f"HAPPY{n + 1}", (_HAPPY_TARGETS[n] if n < len(_HAPPY_TARGETS) else None)
    return f"BTN_{code:03X}", None


def layout_from_spec(spec: EvdevSpec, vid: int, pid: int, name: str,
                     range_known: bool = True) -> DeviceLayout:
    """Розкладка (generated=True) для синтетичного звіту пристрою."""
    codes = {a[0] for a in spec.axes}
    right_pair = (ABS_RX, ABS_RY) if {ABS_RX, ABS_RY} <= codes else \
        ((ABS_Z, ABS_RZ) if {ABS_Z, ABS_RZ} <= codes and not {ABS_RX, ABS_RY} & codes else None)
    triggers = {ABS_Z: "left", ABS_RZ: "right"} if right_pair == (ABS_RX, ABS_RY) else {}
    fnames = {ABS_X: "LX", ABS_Y: "LY"}
    if right_pair:
        fnames[right_pair[0]], fnames[right_pair[1]] = "RX", "RY"
    if triggers:
        fnames[ABS_Z], fnames[ABS_RZ] = "L2", "R2"
    analog_triggers = bool(triggers)

    buttons: Dict[str, dict] = {}
    for i, code in enumerate(spec.keys):
        src, target = _key_name(code)
        if analog_triggers and target in ("LT", "RT"):
            target = None                             # цифрова копія аналогового тригера — не дублюємо
        buttons[src] = {"byte": 1 + i // 8, "bit": i % 8, "target": target}
    fields: Dict[str, dict] = {}
    for code, off, lo, hi in spec.axes:
        fname = fnames.get(code, f"ABS_{code:02X}")
        d = {"offset": off, "fmt": "s16le", "raw_min": lo, "raw_max": hi}
        if fname in ("LX", "LY", "RX", "RY"):
            d["center"] = 0 if lo < 0 < hi else (lo + hi) / 2
        fields[fname] = d
    d: dict = {
        "name": f"{name or 'gamepad'} (evdev)",
        "match": {"vid": f"{vid:04x}", "pid": f"{pid:04x}"},
        "report_id": REPORT_ID, "read_size": spec.size,
        "buttons": buttons, "fields": fields,
    }
    if spec.hat:
        d["hat"] = {"byte": spec.hat_byte, "shift": 0, "mask": 0x0F, "first": 0}
    sticks = {}
    if "LX" in fields and "LY" in fields:
        sticks["left"] = ["LX", "LY"]
    if "RX" in fields and "RY" in fields:
        sticks["right"] = ["RX", "RY"]
    if sticks:
        d["sticks"] = sticks
    if triggers:
        d["triggers"] = {"left": "L2", "right": "R2"}
    lay = DeviceLayout.from_dict(d, f"auto_evdev_{vid:04x}_{pid:04x}")
    lay.generated = True
    notes = ["Пристрій читається через evdev: імена кнопок — стандартні коди ядра (SOUTH=A, EAST=B, "
             "NORTH=X, WEST=Y, TL/TR=LB/RB ...), тож мапінг не є здогадкою."]
    if not range_known:
        notes.append("Діапазони осей не вдалося прочитати (немає прав на /dev/input/event*): "
                     "припущено повний int16.")
    extra = [s for s, t in ((_key_name(c)) for c in spec.keys) if t is None and s not in ("TL2", "TR2")]
    if extra:
        notes.append("Кнопки без вихідної цілі: " + ", ".join(extra))
    if not spec.hat and not any(c in spec.keys for c in (0x220, 0x221, 0x222, 0x223)):
        notes.append("D-pad не знайдено (ні ABS_HAT0, ні BTN_DPAD_*).")
    lay.notes = notes
    return lay
