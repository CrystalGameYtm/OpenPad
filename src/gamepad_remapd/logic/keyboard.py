"""Keyboard as an input of the virtual gamepad: key map ("keymap") -> synthetic report.

A keyboard is not a gamepad, so, as with evdev gamepads (logic/evdev_layout.py), its state is packed
into a "synthetic report" of fixed format, and the same pipeline then runs (layout -> profile ->
deadzone/curves -> uinput/ViGEm):

    byte 0                 Report ID = 0xE6
    bytes 1..N             bit mask of digital buttons (order — `KeyMap.buttons`)
    then, if present:      LX LY (s16le, center 0), RX RY (s16le), L2 (u8), R2 (u8)

Keymap (JSON; built-in — keymaps/default.json, user ones — in <config>/keymaps/*.json):

    {"name": "default",
     "keys": {"W": "LS_UP", "SPACE": "A", "Z": "LT", ...},
     "options": {"walk_key": "LEFTALT", "walk_scale": 0.5, "circle": true}}

The left side is a key name ("W", "KEY_W", "LEFTSHIFT", "UP"); the right side is a target: a gamepad button
(A B X Y LB RB BACK START GUIDE LS RS UP DOWN LEFT RIGHT M1..M6 C Z ML MR), a full trigger press (LT, RT),
or a stick direction (LS_UP LS_DOWN LS_LEFT LS_RIGHT RS_UP RS_DOWN RS_LEFT RS_RIGHT).

Sticks: opposite keys (A+D) — the one pressed LAST wins (like SOCD "last input"); a diagonal is
normalized to the circle, so diagonal movement is not 1.41× faster; holding `walk_key`
scales the left axis down to `walk_scale` (slow walk).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ..constants import DPAD_BUTTONS, EXTRA_BUTTONS, STANDARD_BUTTONS
from .layout import DeviceLayout
from ..i18n import tr

REPORT_ID = 0xE6
STICK_MAX = 32767

# --- key names (linux/input-event-codes.h; only the main ones — enough for game layouts) ---
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
_BUTTON_SOURCE = {t: (f"PAD_{t}" if t in DPAD_BUTTONS else t) for t in BUTTON_TARGETS}   # DPAD_* is reserved for the hat


class KeyMapError(ValueError):
    pass


def key_code(name: Union[str, int]) -> int:
    """"W", "KEY_W", "leftshift" or a number -> evdev code."""
    if isinstance(name, int) and not isinstance(name, bool):
        return name
    n = str(name).strip().upper().replace(" ", "")
    if n.startswith("KEY_"):
        n = n[4:]
    if n in _KEYS:
        return _KEYS[n]
    raise KeyMapError(tr('unknown key {0!r} (examples: W, SPACE, LEFTSHIFT, UP, F5, KP4)', name))


@dataclass(frozen=True)
class KeyMap:
    name: str
    keys: Dict[int, str]                       
    walk_key: Optional[int] = None
    walk_scale: float = 0.5
    circle: bool = True
    buttons: Tuple[str, ...] = ()              

    @classmethod
    def from_dict(cls, d: dict, fallback_name: str = "keymap") -> "KeyMap":
        if not isinstance(d, dict) or not isinstance(d.get("keys"), dict) or not d["keys"]:
            raise KeyMapError(tr('keymap: a non-empty "keys" object is required'))
        keys: Dict[int, str] = {}
        for k, v in d["keys"].items():
            if k.startswith("_"):
                continue
            code = key_code(k)
            t = str(v).strip().upper()
            if t not in BUTTON_TARGETS and t not in STICK_TARGETS and t not in TRIGGER_TARGETS:
                raise KeyMapError(tr('keys.{0}: unknown target {1!r}; allowed buttons {2}, triggers LT/RT, sticks {3}', k, v, list(BUTTON_TARGETS), list(STICK_TARGETS)))
            if code in keys:
                raise KeyMapError(tr('keys.{0}: key specified twice', k))
            keys[code] = t
        opts = d.get("options") or {}
        if not isinstance(opts, dict):
            raise KeyMapError(tr('options must be an object'))
        walk_key = key_code(opts["walk_key"]) if opts.get("walk_key") else None
        scale = opts.get("walk_scale", 0.5)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0.05 <= scale <= 1.0:
            raise KeyMapError(tr('options.walk_scale: a number 0.05..1.0'))
        circle = opts.get("circle", True)
        if not isinstance(circle, bool):
            raise KeyMapError("options.circle: true/false")
        used = {v for v in keys.values() if v in BUTTON_TARGETS}
        buttons = tuple(t for t in BUTTON_TARGETS if t in used)
        return cls(str(d.get("name") or fallback_name), keys, walk_key, float(scale), circle, buttons)


def load_keymap(spec: Union[str, dict, None], dirs: Sequence[Path]) -> KeyMap:
    """spec: a file name (without .json) from the dirs (later ones override), or a built-in dict."""
    if isinstance(spec, dict):
        return KeyMap.from_dict(spec, "inline")
    name = spec or "default"
    found: Optional[Path] = None
    for d in dirs:
        p = Path(d) / f"{name}.json"
        if p.is_file():
            found = p
    if found is None:
        raise KeyMapError(tr('keymap {0!r} not found in {1}', name, [str(d) for d in dirs]))
    try:
        return KeyMap.from_dict(json.loads(found.read_text(encoding="utf-8")), name)
    except json.JSONDecodeError as e:
        raise KeyMapError(tr('{0}: invalid JSON: {1}', found, e)) from None


# --------------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Fmt:
    size: int
    sticks: Dict[str, int]               # "L"/"R" -> offset of the first axis (X; Y = +2)
    triggers: Dict[str, int]             # "L2"/"R2" -> offset


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
    lay.notes = [tr('Keyboard as gamepad: the key state is packed into a synthetic report according to the keymap “%s”.') % km.name]
    return lay


class KeyPacker:
    """Pressed-keys state -> synthetic report. No I/O: clean and fast, easy to test."""

    def __init__(self, km: KeyMap):
        self.km = km
        self.fmt = _format(km)
        self._bit = {t: i for i, t in enumerate(km.buttons)}
        self._down: Dict[int, int] = {}               
        self._seq = 0
        self.size = self.fmt.size

    def press(self, code: int) -> bool:
        """True if this changed the report (the key is known to the keymap)."""
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
        raise KeyMapError(tr('toggle: at least one key is required'))
    return codes
