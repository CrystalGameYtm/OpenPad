"""Layout for evdev gamepads (XInput mode, xpad, hid-playstation, hid-nintendo ...).

The Linux kernel has already translated such a device into standard event codes (BTN_SOUTH, ABS_X ...), so
there is NO heuristic by button order here, as with HID descriptors: BTN_SOUTH is always "A/cross",
BTN_TL is LB, and so on. So that the rest of the pipeline (ReportParser, profiles, UI) works unchanged, the
device state is packed into a "synthetic report" of fixed format:

    byte 0              Report ID = 0xE5
    bytes 1..N          button bit mask (in ascending evdev code order)
    next byte           hat D-pad (ABS_HAT0X/Y): 0..7 clockwise from "up", 8 = neutral
    then 2 bytes each   axes, int16 little-endian (in ascending ABS_* code order)

`spec_from_caps()` is a deterministic function of the device capabilities; both the layout and the
packer in input/evdev_linux.py take its result, so the format always matches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .layout import DeviceLayout
from ..i18n import tr

REPORT_ID = 0xE5
S16_MIN, S16_MAX = -32768, 32767

# --- evdev codes (linux/input-event-codes.h) ---
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11



BUTTONS: Dict[int, Tuple[str, Optional[str]]] = {
    0x130: ("SOUTH", "A"), 0x131: ("EAST", "B"), 0x132: ("C", "C"),
    0x133: ("NORTH", "X"), 0x134: ("WEST", "Y"), 0x135: ("Z", "Z"),
    0x136: ("TL", "LB"), 0x137: ("TR", "RB"), 0x138: ("TL2", "LT"), 0x139: ("TR2", "RT"),
    0x13A: ("SELECT", "BACK"), 0x13B: ("START", "START"), 0x13C: ("MODE", "GUIDE"),
    0x13D: ("THUMBL", "LS"), 0x13E: ("THUMBR", "RS"),
    0x220: ("PAD_UP", "UP"), 0x221: ("PAD_DOWN", "DOWN"),
    0x222: ("PAD_LEFT", "LEFT"), 0x223: ("PAD_RIGHT", "RIGHT"),
}
_HAPPY_BASE = 0x2C0                       # BTN_TRIGGER_HAPPY1..; paddles/back buttons -> M1..M6, C, Z
_HAPPY_TARGETS = ("M1", "M2", "M3", "M4", "M5", "M6")
GAMEPAD_KEY_RANGES = ((0x120, 0x140), (0x220, 0x224))


@dataclass(frozen=True)
class EvdevSpec:
    keys: Tuple[int, ...]                       
    hat: Optional[Tuple[int, int]]              # (ABS_HAT0X, ABS_HAT0Y) or None
    hat_byte: int
    axes: Tuple[Tuple[int, int, int, int], ...]  # (ABS code, offset in the report, min, max)
    size: int                                   


def is_gamepad(keys, abs_codes) -> bool:
    """Gamepad/joystick: has buttons from the BTN_JOYSTICK/GAMEPAD range (or D-pad buttons) and axes.
        Mice (BTN_LEFT), touchpads and tablets (BTN_TOUCH/STYLUS) are not included.
    """
    has_btn = any(lo <= k < hi for k in keys for lo, hi in GAMEPAD_KEY_RANGES)
    has_axes = (ABS_X in abs_codes and ABS_Y in abs_codes) or ABS_HAT0X in abs_codes
    return has_btn and has_axes


def spec_from_caps(keys, abs_info: Dict[int, Tuple[int, int]]) -> EvdevSpec:
    """keys — the set of button codes; abs_info — {ABS code: (min, max)}."""
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
            continue                                  
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
    """Layout (generated=True) for the device's synthetic report."""
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
            target = None                             
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
    notes = [tr('The device is read through evdev: button names are standard kernel codes (SOUTH=A, EAST=B, NORTH=X, WEST=Y, TL/TR=LB/RB ...), so the mapping is not a guess.')]
    if not range_known:
        notes.append(tr('Could not read the axis ranges (no permission for /dev/input/event*): full int16 assumed.'))
    extra = [s for s, t in ((_key_name(c)) for c in spec.keys) if t is None and s not in ("TL2", "TR2")]
    if extra:
        notes.append(tr('Buttons with no output target: ') + ", ".join(extra))
    if not spec.hat and not any(c in spec.keys for c in (0x220, 0x221, 0x222, 0x223)):
        notes.append(tr('D-pad not found (neither ABS_HAT0 nor BTN_DPAD_*).'))
    lay.notes = notes
    return lay
