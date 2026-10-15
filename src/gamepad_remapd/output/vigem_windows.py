"""Windows: віртуальний Xbox 360 через ViGEmBus (бібліотека `vgamepad`).

УВАГА: модуль написано за документацією vgamepad/ViGEm, але в Linux-середовищі
розробки його запустити неможливо — на Windows його потрібно перевірити.
Вимоги: встановлений драйвер ViGEmBus та `pip install vgamepad`.

Особливості XInput:
  * Додаткових кнопок (M1–M6/C/Z) у XUSB-звіті немає -> `button_id` повертає None,
    такі цілі пропускаються (див. warnings у профілі).
  * XInput має Y-вісь «вгору = плюс», тоді як канонічні одиниці Pipeline — як
    в evdev (вгору = мінус), тому знак Y інвертується тут.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .base import OutputBackend

log = logging.getLogger(__name__)

# XINPUT_GAMEPAD_* маски (стабільні значення Windows SDK).
_XUSB: Dict[str, int] = {
    "UP": 0x0001, "DOWN": 0x0002, "LEFT": 0x0004, "RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LS": 0x0040, "RS": 0x0080,
    "LB": 0x0100, "RB": 0x0200, "GUIDE": 0x0400,
    "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}
_AXES = ("LX", "LY", "RX", "RY", "LT", "RT")


class VigemPad(OutputBackend):
    kind = "vigem"

    def __init__(self, name: Optional[str] = None):  # name: не використовується (ViGEm фіксує ідентичність)
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
        import vgamepad as vg  # імпорт тут: на Linux модуль недоступний

        self._pad = vg.VX360Gamepad()
        self._report = self._pad.report  # XUSB_REPORT: пишемо поля напряму, без обгорток
        # Зворотний зв'язок: драйвер викликає callback із ЧУЖОГО потоку при rumble від гри.
        self._pad.register_notification(callback_function=self._notify)
        log.info("ViGEm: створено віртуальний Xbox 360 контролер")

    def close(self) -> None:
        self._pad = None  # деструктор vgamepad від'єднує ціль від шини

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
            r.sThumbLY = 32767 if value <= -32768 else -value   # інверсія Y
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
        if self._dirty:
            self._pad.update()      # один IOCTL до драйвера ViGEmBus
            self._dirty = False

    def release_all(self) -> None:
        self._buttons = 0
        self._pad.reset()
        # reset() у vgamepad може підмінити об'єкт звіту на новий: беремо посилання
        # знову, інакше після гарячої зміни профілю set_axis писав би в «мертвий» звіт.
        self._report = self._pad.report
        self._pad.update()
        self._dirty = False
