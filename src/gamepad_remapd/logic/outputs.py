"""Which output protocols can be offered for a device: XInput emulation or the "native" mode."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from ..i18n import tr

SONY_VID = 0x054C


def native_kind(vid: int, layout_id: Optional[str]) -> str:
    """DualShock-compatible (Sony VID or a sony_ds4*/ds4 layout) -> "ds4", all others -> generic "dinput"."""
    if vid == SONY_VID or (layout_id and "ds4" in layout_id.lower()):
        return "ds4"
    return "dinput"


def output_choices(vid: int, layout_id: Optional[str], windows: Optional[bool] = None) -> List[Dict[str, Any]]:
    windows = (os.name == "nt") if windows is None else windows
    xin = {"id": "xbox360", "label": tr('XInput emulation (Xbox 360)'),
           "note": tr('Suits almost all games (XInput/SDL). Recommended by default.')}
    if windows:
        return [dict(xin, note=tr('On Windows the output is a virtual Xbox 360 (ViGEm).'))]
    kind = native_kind(vid, layout_id)
    nat = ({"id": "ds4", "label": tr('Native: DualShock 4'),
            "note": tr('Virtual DS4 (Sony 054c:09cc).'),
            "warn": tr('Experimental: SDL with HIDAPI hands gamepads with a DualShock VID:PID to its own driver and may not show such a device. For emulators and SDL games choose XInput.')}
           if kind == "ds4" else
           {"id": "dinput", "label": tr('Native: DInput (generic gamepad)'),
            "note": tr('Classic evdev joystick (BTN_TRIGGER… buttons, X/Y/Z/RZ axes) — just like a DInput gamepad itself.'),
            "warn": tr('Experimental, does NOT work in most games: SDL has no mapping for such a device, so the game/emulator does not recognize it as a gamepad (or mixes up axes and buttons). For games and emulators choose “XInput emulation”.')})
    return [xin, nat]


def warn_for(choices: List[Dict[str, Any]], kind: str) -> Optional[str]:
    for c in choices:
        if c["id"] == kind:
            return c.get("warn")
    return None


def pick_kind(wanted: Optional[str], choices: List[Dict[str, Any]], fallback: str) -> str:
    ids = [c["id"] for c in choices]
    for k in (wanted, fallback):
        if k in ids:
            return k          # type: ignore[return-value]
    return ids[0]
