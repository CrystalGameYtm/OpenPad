"""Фабрика вихідних backend-ів (імпорти ліниві: платформні залежності)."""
from __future__ import annotations

import os
from typing import Optional

from .base import OutputBackend


def create_output(kind: str = "xbox360", name: Optional[str] = None,
                  uinput_path: str = "/dev/uinput", dpad_mode: str = "hat") -> OutputBackend:
    """kind: "xbox360" | "ds4" (Linux/uinput) ; на Windows завжди ViGEm X360."""
    if os.name == "nt":
        from .vigem_windows import VigemPad
        return VigemPad(name)
    from .uinput_linux import SPECS, UinputPad
    if kind not in SPECS:
        raise ValueError(f"невідомий output.kind {kind!r}; доступні {sorted(SPECS)}")
    if dpad_mode not in ("hat", "buttons"):
        raise ValueError(f"dpad_mode: 'hat' або 'buttons', отримано {dpad_mode!r}")
    return UinputPad(SPECS[kind], path=uinput_path, name=name, dpad_buttons=(dpad_mode == "buttons"))
