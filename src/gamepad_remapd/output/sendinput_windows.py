"""Windows: keyboard and mouse buttons through SendInput (`KEY_*` / `MOUSE_*` targets in button_map).

Keys are sent as SCAN CODES (KEYEVENTF_SCANCODE), like a real keyboard: this way both ordinary programs and games
using DirectInput/RawInput see them. The evdev codes for the main keys (1..88: Esc, digits, letters, Fn, modifiers)
match the PS/2 set 1 scan codes; the extended ones (arrows, Home/End, right Ctrl/Alt, Win, numpad Enter and /) use the same
scan codes with the EXTENDEDKEY flag; media keys, Pause and F13–F24 go by virtual key code (VK).

Changes of a frame are collected and delivered in ONE SendInput call. The held-keys state is tracked, so
`release_all()` never leaves a key stuck. The module also imports on Linux (the structures are plain ctypes);
only the default call needs Windows, and tests substitute `send`.

WARNING: written from the WinAPI documentation; not yet run on Windows.
"""
from __future__ import annotations

import ctypes
import logging
from typing import Callable, List, Optional, Set, Tuple
from ..i18n import tr

log = logging.getLogger(__name__)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
MOUSEEVENTF = {  # evdev BTN_* -> (down, up, mouseData)
    0x110: (0x0002, 0x0004, 0),        # LEFT
    0x111: (0x0008, 0x0010, 0),        # RIGHT
    0x112: (0x0020, 0x0040, 0),        # MIDDLE
    0x113: (0x0080, 0x0100, 1),        # SIDE   = XBUTTON1
    0x114: (0x0080, 0x0100, 2),        # EXTRA  = XBUTTON2
}

# evdev code -> (set 1 scan code, extended)
_EXT = {96: 0x1C, 97: 0x1D, 98: 0x35, 100: 0x38, 99: 0x37, 102: 0x47, 103: 0x48, 104: 0x49, 105: 0x4B,
        106: 0x4D, 107: 0x4F, 108: 0x50, 109: 0x51, 110: 0x52, 111: 0x53, 125: 0x5B, 126: 0x5C, 139: 0x5D}
# evdev code -> Windows virtual key code (keys without a reliable scan code)
_VK = {119: 0x13,                                  # Pause
       113: 0xAD, 114: 0xAE, 115: 0xAF,            # Mute, Volume Down, Volume Up
       163: 0xB0, 165: 0xB1, 166: 0xB2, 164: 0xB3,  # Next, Previous, Stop, Play/Pause
       **{183 + i: 0x7C + i for i in range(12)}}   # F13..F24


class _KeybdInput(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeybdInput), ("mi", _MouseInput)]


class Input(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _InputUnion)]


def key_event(code: int, down: bool) -> Optional[Input]:
    """SendInput event for a keyboard key/mouse button with the given evdev code (None — unknown code)."""
    ev = Input()
    if code in MOUSEEVENTF:
        dn, up, data = MOUSEEVENTF[code]
        ev.type = INPUT_MOUSE
        ev.mi.dwFlags = dn if down else up
        ev.mi.mouseData = data
        return ev
    ev.type = INPUT_KEYBOARD
    flags = 0 if down else KEYEVENTF_KEYUP
    if code in _VK:
        ev.ki.wVk = _VK[code]
        ev.ki.dwFlags = flags
    elif code in _EXT:
        ev.ki.wScan = _EXT[code]
        ev.ki.dwFlags = flags | KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY
    elif code == 69:                               # NumLock: scan 0x45 + extended (otherwise it collides with Pause)
        ev.ki.wScan = 0x45
        ev.ki.dwFlags = flags | KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY
    elif 1 <= code <= 88:
        ev.ki.wScan = code
        ev.ki.dwFlags = flags | KEYEVENTF_SCANCODE
    else:
        return None
    return ev


def _default_send(events) -> int:
    arr = (Input * len(events))(*events)
    return ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(Input))   # type: ignore[attr-defined]


class WinKeyboard:
    def __init__(self, send: Optional[Callable[[List[Input]], int]] = None):
        self._send = send or _default_send
        self._pending: List[Input] = []
        self._held: Set[int] = set()
        self.unmapped: Set[int] = set()

    def set_key(self, code: int, down: bool) -> None:
        ev = key_event(code, down)
        if ev is None:
            if code not in self.unmapped:
                self.unmapped.add(code)
                log.warning(tr('SendInput: code %d has no equivalent on Windows'), code)
            return
        if down:
            self._held.add(code)
        else:
            self._held.discard(code)
        self._pending.append(ev)

    def move(self, dx: int, dy: int) -> None:
        ev = Input()
        ev.type = INPUT_MOUSE
        ev.mi.dx, ev.mi.dy = int(dx), int(dy)
        ev.mi.dwFlags = 0x0001                       # MOUSEEVENTF_MOVE (relative motion)
        self._pending.append(ev)

    def flush(self) -> None:
        if self._pending:
            events, self._pending = self._pending, []
            self._send(events)

    def release_all(self) -> None:
        for code in sorted(self._held):
            self.set_key(code, False)
        self.flush()
