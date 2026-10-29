"""«Сухий» вихід без системних викликів: компіляція профілів (`check`) і замір Pipeline (`bench`) на ОС,
де нема uinput (Windows). Має ті самі імена кнопок, що й віртуальний Xbox 360; лише рахує події."""
from __future__ import annotations

from typing import Optional

from .base import OutputBackend
from .vigem_windows import _AXES, _XUSB


class DryPad(OutputBackend):
    kind = "dry"
    supports_keys = True

    def __init__(self) -> None:
        self._names = list(_XUSB)
        self._ids = {n: i for i, n in enumerate(self._names)}
        self.n_buttons = len(self._names)
        self.events = 0

    def open(self) -> None: ...
    def close(self) -> None: ...
    def button_id(self, name: str) -> Optional[int]: return self._ids.get(name)
    def axis_id(self, name: str) -> int: return _AXES.index(name)
    def set_button(self, bid: int, down: bool) -> None: self.events += 1
    def set_axis(self, aid: int, value: int) -> None: self.events += 1
    def set_key(self, code: int, down: bool) -> None: self.events += 1
    def flush(self) -> None: ...
    def release_all(self) -> None: ...
