"""Windows: a virtual Xbox 360 through ViGEmBus (the `vgamepad` library).

WARNING: this module was written from the vgamepad/ViGEm documentation, but it cannot be run in the Linux development
environment — it must be verified on Windows. Requirements: the ViGEmBus driver installed and `pip install vgamepad`.

The keyboard and mouse (`KEY_*`/`MOUSE_*` targets in button_map) are emitted through SendInput (output/sendinput_windows.py).

XInput specifics:
  * There are no extra buttons (M1–M6/C/Z) in the XUSB report -> `button_id` returns None, and such
    targets are skipped (see the warnings in the profile).
  * XInput has the Y axis "up = plus", while the canonical units of the Pipeline are as in
    evdev (up = minus), so the sign of Y is inverted here.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .base import OutputBackend
from ..i18n import tr

log = logging.getLogger(__name__)

# XINPUT_GAMEPAD_* masks (stable values from the Windows SDK).
_XUSB: Dict[str, int] = {
    "UP": 0x0001, "DOWN": 0x0002, "LEFT": 0x0004, "RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LS": 0x0040, "RS": 0x0080,
    "LB": 0x0100, "RB": 0x0200, "GUIDE": 0x0400,
    "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}
_AXES = ("LX", "LY", "RX", "RY", "LT", "RT")


class VigemPad(OutputBackend):
    kind = "vigem"
    supports_keys = True

    def __init__(self, name: Optional[str] = None, send_fn=None):  # name: not used (ViGEm fixes the identity)
        self._send_fn = send_fn                      
        self._kbd = None
        self._pad = None
        self._report = None
        self._names: List[str] = list(_XUSB)
        self._ids = {n: i for i, n in enumerate(self._names)}
        self._masks = [_XUSB[n] for n in self._names]
        self.n_buttons = len(self._names)
        self._buttons = 0
        self._dirty = False
        self.on_rumble = None

    def open(self) -> None:
        import vgamepad as vg  

        self._pad = vg.VX360Gamepad()
        self._report = self._pad.report  # XUSB_REPORT: we write the fields directly, without wrappers
        
        self._pad.register_notification(callback_function=self._notify)
        log.info(tr('ViGEm: virtual Xbox 360 controller created'))

    def ensure_keys(self) -> None:
        if self._kbd is None:
            from .sendinput_windows import WinKeyboard
            self._kbd = WinKeyboard(self._send_fn)

    def set_key(self, code: int, down: bool) -> None:
        if self._kbd is not None:
            self._kbd.set_key(code, down)

    def move_mouse(self, dx: int, dy: int) -> None:
        if self._kbd is not None:
            self._kbd.move(dx, dy)

    def close(self) -> None:
        if self._kbd is not None:
            self._kbd.release_all()
            self._kbd = None
        self._pad = None  

    def _notify(self, client, target, large_motor, small_motor, led_number, user_data) -> None:
        cb = self.on_rumble
        if cb is not None:
            cb(large_motor * 257, small_motor * 257)   # 0..255 -> 0..65535

    def button_id(self, name: str) -> Optional[int]:
        return self._ids.get(name)

    def axis_id(self, name: str) -> int:
        return _AXES.index(name)

    def set_button(self, bid: int, down: bool) -> None:
        if down:
            self._buttons |= self._masks[bid]
        else:
            self._buttons &= ~self._masks[bid]
        self._report.wButtons = self._buttons
        self._dirty = True

    def set_axis(self, aid: int, value: int) -> None:
        r = self._report
        if aid == 0:
            r.sThumbLX = value
        elif aid == 1:
            r.sThumbLY = 32767 if value <= -32768 else -value   
        elif aid == 2:
            r.sThumbRX = value
        elif aid == 3:
            r.sThumbRY = 32767 if value <= -32768 else -value
        elif aid == 4:
            r.bLeftTrigger = value
        else:
            r.bRightTrigger = value
        self._dirty = True

    def flush(self) -> None:
        if self._kbd is not None:
            self._kbd.flush()       
        if self._dirty:
            self._pad.update()      
            self._dirty = False

    def release_all(self) -> None:
        if self._kbd is not None:
            self._kbd.release_all()                  
        self._buttons = 0
        if self._pad is None:
            return
        self._pad.reset()
        # vgamepad's reset() may replace the report object with a new one: we take the reference each time
        
        self._report = self._pad.report
        self._pad.update()
        self._dirty = False
