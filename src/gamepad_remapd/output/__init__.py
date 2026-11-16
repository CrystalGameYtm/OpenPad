"""Output backend factory (imports are lazy: platform dependencies)."""
from __future__ import annotations

import os
from typing import Optional

from .base import OutputBackend
from ..i18n import tr


def create_output(kind: str = "xbox360", name: Optional[str] = None,
                  uinput_path: str = "/dev/uinput", dpad_mode: str = "hat") -> OutputBackend:
    """kind: "xbox360" | "ds4" | "dinput" (Linux/uinput); on Windows it is always ViGEm X360."""
    if os.name == "nt":
        from .vigem_windows import VigemPad
        return VigemPad(name)
    from .uinput_linux import SPECS, UinputPad
    if kind not in SPECS:
        raise ValueError(tr('unknown output.kind {0!r}; available {1}', kind, sorted(SPECS)))
    if dpad_mode not in ("hat", "buttons"):
        raise ValueError(tr("dpad_mode: 'hat' or 'buttons', got {0!r}", dpad_mode))
    return UinputPad(SPECS[kind], path=uinput_path, name=name, dpad_buttons=(dpad_mode == "buttons"))
