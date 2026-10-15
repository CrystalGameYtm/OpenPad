"""«Порожній» вихід для режиму лише-sniffing: сесія читає пристрій, але
віртуального геймпада не створює (щоб не плодити дублікатів під час дослідження)."""
from __future__ import annotations

from typing import Optional

from .base import OutputBackend


class NullPad(OutputBackend):
    kind = "null"
    n_buttons = 0

    def open(self) -> None: ...
    def close(self) -> None: ...
    def button_id(self, name: str) -> Optional[int]: return None
    def axis_id(self, name: str) -> int: return 0
    def set_button(self, bid: int, down: bool) -> None: ...
    def set_axis(self, aid: int, value: int) -> None: ...
    def flush(self) -> None: ...
    def release_all(self) -> None: ...
