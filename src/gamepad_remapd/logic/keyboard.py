"""Клавіатура як вхід віртуального геймпада: розкладка клавіш («keymap») -> синтетичний звіт.

Клавіатура не є геймпадом, тож, як і для evdev-геймпадів (logic/evdev_layout.py), її стан пакується в
«синтетичний звіт» фіксованого формату, і далі працює той самий конвеєр (розкладка -> профіль ->
deadzone/криві -> uinput/ViGEm):

    байт 0                 Report ID = 0xE6
    байти 1..N             бітова маска цифрових кнопок (порядок — `KeyMap.buttons`)
    далі, за наявності:    LX LY (s16le, центр 0), RX RY (s16le), L2 (u8), R2 (u8)

Keymap (JSON; вбудований — keymaps/default.json, свої — у <конфіг>/keymaps/*.json):

    {"name": "default",
     "keys": {"W": "LS_UP", "SPACE": "A", "Z": "LT", ...},
     "options": {"walk_key": "LEFTALT", "walk_scale": 0.5, "circle": true}}

Ліва частина — ім'я клавіші («W», «KEY_W», «LEFTSHIFT», «UP»), права — ціль: кнопка геймпада
(A B X Y LB RB BACK START GUIDE LS RS UP DOWN LEFT RIGHT M1..M6 C Z ML MR), повне віддавлення
тригера (LT, RT) або напрямок стіка (LS_UP LS_DOWN LS_LEFT LS_RIGHT RS_UP RS_DOWN RS_LEFT RS_RIGHT).

Стіки: протилежні клавіші (A+D) — перемагає та, що натиснута ОСТАННЬОЮ (як SOCD «last input»); діагональ
нормується до кола (circle), щоб рух по діагоналі не був у 1.41 раза швидшим; утримання `walk_key`
зменшує ліву вісь до `walk_scale` (повільна хода).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ..constants import DPAD_BUTTONS, EXTRA_BUTTONS, STANDARD_BUTTONS
from .layout import DeviceLayout

REPORT_ID = 0xE6
STICK_MAX = 32767

# --- імена клавіш (linux/input-event-codes.h; лише основні — для ігрових розкладок вистачає) ---
_KEYS: Dict[str, int] = {"ESC": 1, "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14, "TAB": 15, "LEFTBRACE": 26,
                         "RIGHTBRACE": 27, "ENTER": 28, "LEFTCTRL": 29, "SEMICOLON": 39, "APOSTROPHE": 40,
                         "GRAVE": 41, "LEFTSHIFT": 42, "BACKSLASH": 43, "COMMA": 51, "DOT": 52, "SLASH": 53,
                         "RIGHTSHIFT": 54, "KPASTERISK": 55, "LEFTALT": 56, "SPACE": 57, "CAPSLOCK": 58,
                         "NUMLOCK": 69, "SCROLLLOCK": 70, "KPMINUS": 74, "KPPLUS": 78, "KPDOT": 83,
                         "KPENTER": 96, "RIGHTCTRL": 97, "KPSLASH": 98, "RIGHTALT": 100, "HOME": 102,
                         "UP": 103, "PAGEUP": 104, "LEFT": 105, "RIGHT": 106, "END": 107, "DOWN": 108,
                         "PAGEDOWN": 109, "INSERT": 110, "DELETE": 111, "PAUSE": 119, "LEFTMETA": 125,
                         "RIGHTMETA": 126}
for _i, _c in enumerate("1234567890"):
    _KEYS[_c] = 2 + _i
for _i, _c in enumerate("QWERTYUIOP"):
    _KEYS[_c] = 16 + _i
for _i, _c in enumerate("ASDFGHJKL"):
    _KEYS[_c] = 30 + _i
for _i, _c in enumerate("ZXCVBNM"):
    _KEYS[_c] = 44 + _i
for _i in range(1, 11):
    _KEYS[f"F{_i}"] = 58 + _i
_KEYS["F11"], _KEYS["F12"] = 87, 88
for _i, _n in enumerate(("7", "8", "9"), 71):
    _KEYS["KP" + _n] = _i
for _i, _n in enumerate(("4", "5", "6"), 75):
    _KEYS["KP" + _n] = _i
for _i, _n in enumerate(("1", "2", "3", "0"), 79):
    _KEYS["KP" + _n] = _i
KEY_NAMES: Dict[int, str] = {v: k for k, v in _KEYS.items()}

STICK_TARGETS = {"LS_UP": ("L", 1, -1), "LS_DOWN": ("L", 1, +1), "LS_LEFT": ("L", 0, -1), "LS_RIGHT": ("L", 0, +1),
                 "RS_UP": ("R", 1, -1), "RS_DOWN": ("R", 1, +1), "RS_LEFT": ("R", 0, -1), "RS_RIGHT": ("R", 0, +1)}
TRIGGER_TARGETS = {"LT": "L2", "RT": "R2"}
BUTTON_TARGETS = STANDARD_BUTTONS + DPAD_BUTTONS + EXTRA_BUTTONS
_BUTTON_SOURCE = {t: (f"PAD_{t}" if t in DPAD_BUTTONS else t) for t in BUTTON_TARGETS}   # DPAD_* зарезервоване для hat


class KeyMapError(ValueError):
    pass


def key_code(name: Union[str, int]) -> int:
    """«W», «KEY_W», «leftshift» або число -> код evdev."""
    if isinstance(name, int) and not isinstance(name, bool):
        return name
    n = str(name).strip().upper().replace(" ", "")
    if n.startswith("KEY_"):
        n = n[4:]
    if n in _KEYS:
        return _KEYS[n]
    raise KeyMapError(f"невідома клавіша {name!r} (приклади: W, SPACE, LEFTSHIFT, UP, F5, KP4)")


@dataclass(frozen=True)
class KeyMap:
    name: str
    keys: Dict[int, str]                       # код клавіші -> ціль
    walk_key: Optional[int] = None
    walk_scale: float = 0.5
    circle: bool = True
    buttons: Tuple[str, ...] = ()              # цілі-кнопки, що реально використано (порядок бітів)

    @classmethod
    def from_dict(cls, d: dict, fallback_name: str = "keymap") -> "KeyMap":
        if not isinstance(d, dict) or not isinstance(d.get("keys"), dict) or not d["keys"]:
            raise KeyMapError("keymap: потрібен непорожній об'єкт \"keys\"")
        keys: Dict[int, str] = {}
        for k, v in d["keys"].items():
            if k.startswith("_"):
                continue
            code = key_code(k)
            t = str(v).strip().upper()
            if t not in BUTTON_TARGETS and t not in STICK_TARGETS and t not in TRIGGER_TARGETS:
                raise KeyMapError(f"keys.{k}: невідома ціль {v!r}; дозволені кнопки {list(BUTTON_TARGETS)}, "
                                  f"тригери LT/RT, стіки {list(STICK_TARGETS)}")
            if code in keys:
                raise KeyMapError(f"keys.{k}: клавіша вказана двічі")
            keys[code] = t
        opts = d.get("options") or {}
        if not isinstance(opts, dict):
            raise KeyMapError("options має бути об'єктом")
        walk_key = key_code(opts["walk_key"]) if opts.get("walk_key") else None
        scale = opts.get("walk_scale", 0.5)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0.05 <= scale <= 1.0:
            raise KeyMapError("options.walk_scale: число 0.05..1.0")
        circle = opts.get("circle", True)
        if not isinstance(circle, bool):
            raise KeyMapError("options.circle: true/false")
        used = {v for v in keys.values() if v in BUTTON_TARGETS}
        buttons = tuple(t for t in BUTTON_TARGETS if t in used)
        return cls(str(d.get("name") or fallback_name), keys, walk_key, float(scale), circle, buttons)


def load_keymap(spec: Union[str, dict, None], dirs: Sequence[Path]) -> KeyMap:
    """spec: ім'я файлу (без .json) із каталогів dirs (пізніші перекривають), або вбудований словник."""
    if isinstance(spec, dict):
        return KeyMap.from_dict(spec, "inline")
    name = spec or "default"
    found: Optional[Path] = None
    for d in dirs:
        p = Path(d) / f"{name}.json"
        if p.is_file():
            found = p
    if found is None:
        raise KeyMapError(f"keymap {name!r} не знайдено в {[str(d) for d in dirs]}")
    try:
        return KeyMap.from_dict(json.loads(found.read_text(encoding="utf-8")), name)
    except json.JSONDecodeError as e:
        raise KeyMapError(f"{found}: некоректний JSON: {e}") from None


# --------------------------------------------------------------------------------------------
# Формат синтетичного звіту й розкладка
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Fmt:
    size: int
    sticks: Dict[str, int]               # "L"/"R" -> зсув першої осі (X; Y = +2)
    triggers: Dict[str, int]             # "L2"/"R2" -> зсув


def _format(km: KeyMap) -> _Fmt:
    pos = 1 + (len(km.buttons) + 7) // 8
    sticks: Dict[str, int] = {}
    for side in ("L", "R"):
        if any(t.startswith(side + "S_") for t in km.keys.values()):
            sticks[side] = pos
            pos += 4
    triggers: Dict[str, int] = {}
    for t, f in TRIGGER_TARGETS.items():
        if t in km.keys.values():
            triggers[f] = pos
            pos += 1
    return _Fmt(pos, sticks, triggers)


def build_layout(km: KeyMap, vid: int, pid: int) -> DeviceLayout:
    f = _format(km)
    buttons = {_BUTTON_SOURCE[t]: {"byte": 1 + i // 8, "bit": i % 8, "target": t} for i, t in enumerate(km.buttons)}
    fields: dict = {}
    d: dict = {"name": f"Keyboard Pad ({km.name})", "match": {"vid": f"{vid:04x}", "pid": f"{pid:04x}"},
                "report_id": REPORT_ID, "read_size": f.size, "buttons": buttons, "fields": fields}
    sticks: dict = {}
    for side, off in f.sticks.items():
        for ax, n in enumerate(("X", "Y")):
            fields[f"{side}{n}"] = {"offset": off + 2 * ax, "fmt": "s16le", "raw_min": -STICK_MAX,
                                    "raw_max": STICK_MAX, "center": 0}
        sticks["left" if side == "L" else "right"] = [f"{side}X", f"{side}Y"]
    trig: dict = {}
    for fname, off in f.triggers.items():
        fields[fname] = {"offset": off, "fmt": "u8", "raw_min": 0, "raw_max": 255}
        trig["left" if fname == "L2" else "right"] = fname
    if sticks:
        d["sticks"] = sticks
    if trig:
        d["triggers"] = trig
    lay = DeviceLayout.from_dict(d, f"keyboard_{vid:04x}_{pid:04x}")
    lay.generated = True
    lay.notes = ["Клавіатура як геймпад: стан клавіш пакується в синтетичний звіт за keymap «%s»." % km.name]
    return lay


class KeyPacker:
    """Стан натиснутих клавіш -> синтетичний звіт. Без I/O: чисто й швидко, легко тестується."""

    def __init__(self, km: KeyMap):
        self.km = km
        self.fmt = _format(km)
        self._bit = {t: i for i, t in enumerate(km.buttons)}
        self._down: Dict[int, int] = {}               # код -> порядковий номер натискання (для «останній виграє»)
        self._seq = 0
        self.size = self.fmt.size

    def press(self, code: int) -> bool:
        """True, якщо це змінило звіт (клавіша відома keymap)."""
        if code in self._down or not (code in self.km.keys or code == self.km.walk_key):
            return False
        self._seq += 1
        self._down[code] = self._seq
        return True

    def release(self, code: int) -> bool:
        return self._down.pop(code, None) is not None

    def clear(self) -> None:
        self._down.clear()

    def _axis(self, side: str, ax: int) -> float:
        neg = pos = 0
        for code, seq in self._down.items():
            t = self.km.keys.get(code)
            s = STICK_TARGETS.get(t) if t else None
            if s and s[0] == side and s[1] == ax:
                if s[2] < 0:
                    neg = max(neg, seq)
                else:
                    pos = max(pos, seq)
        if neg and pos:
            return 1.0 if pos > neg else -1.0
        return 1.0 if pos else (-1.0 if neg else 0.0)

    def report(self) -> bytes:
        buf = bytearray(self.size)
        buf[0] = REPORT_ID
        trig_down = set()
        for code in self._down:
            t = self.km.keys.get(code)
            if t in self._bit:
                buf[1 + self._bit[t] // 8] |= 1 << (self._bit[t] % 8)
            elif t in TRIGGER_TARGETS:
                trig_down.add(TRIGGER_TARGETS[t])
        walking = self.km.walk_key is not None and self.km.walk_key in self._down
        for side, off in self.fmt.sticks.items():
            x, y = self._axis(side, 0), self._axis(side, 1)
            if self.km.circle and x and y:
                x, y = x * math.sqrt(0.5), y * math.sqrt(0.5)
            if walking and side == "L":
                x, y = x * self.km.walk_scale, y * self.km.walk_scale
            buf[off:off + 2] = int(round(x * STICK_MAX)).to_bytes(2, "little", signed=True)
            buf[off + 2:off + 4] = int(round(y * STICK_MAX)).to_bytes(2, "little", signed=True)
        for fname, off in self.fmt.triggers.items():
            buf[off] = 255 if fname in trig_down else 0
        return bytes(buf)


def parse_chord(names: Iterable[Union[str, int]]) -> Tuple[int, ...]:
    codes = tuple(key_code(n) for n in names)
    if len(codes) < 1:
        raise KeyMapError("toggle: потрібна принаймні одна клавіша")
    return codes
