"""Virtual gamepad abstraction (Output Layer).

The Pipeline works only through this interface, so the same logic code serves
both Linux (uinput) and Windows (ViGEm).

Performance contract: `set_button` / `set_axis` only WRITE the change into an internal
buffer; the single system call (a write to /dev/uinput or a ViGEm IOCTL) happens in
`flush()` — once per input report. On Linux this means all changes of one report and the
closing SYN_REPORT reach the kernel in ONE `write()`, so the consumer (the game) sees a
consistent state frame.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class OutputBackend(ABC):
    kind: str = "abstract"
    #: Number of "regular" button ids (id 0..n_buttons-1).
    n_buttons: int = 0
    #: Called when the game asks to turn rumble on/off: (strong16, weak16).
    #: May be called from the event loop (Linux) or from a foreign thread (Windows).
    on_rumble: Optional[Callable[[int, int], None]] = None
    #: Whether the backend can emit keyboard/mouse keys (`KEY_*`/`MOUSE_*` targets in button_map).
    supports_keys: bool = False

    @abstractmethod
    def open(self) -> None:
        """Creates the virtual device."""

    @abstractmethod
    def close(self) -> None:
        """Destroys the virtual device."""

    @abstractmethod
    def button_id(self, name: str) -> Optional[int]:
        """Logical button name ("A", "M1", "UP"...) -> numeric id, or None if this backend
        cannot emit such a button.
        """

    @abstractmethod
    def axis_id(self, name: str) -> int:
        """"LX" | "LY" | "RX" | "RY" | "LT" | "RT" -> numeric axis id."""

    @abstractmethod
    def set_button(self, bid: int, down: bool) -> None: ...

    @abstractmethod
    def set_axis(self, aid: int, value: int) -> None:
        """value in canonical units: sticks -32768..32767 (up = minus, as in evdev), triggers 0..255."""

    @abstractmethod
    def flush(self) -> None:
        """Delivers the accumulated changes to the consumer (one system call)."""

    @abstractmethod
    def release_all(self) -> None:
        """Neutral state: all buttons released, axes centered."""

    def attach_loop(self, loop) -> None:  # noqa: B027 - optional hook
        """Attaches the backend to the asyncio loop (to read FF requests)."""

    def ensure_keys(self) -> None:  # noqa: B027 - optional hook
        """Creates the virtual keyboard IN ADVANCE (while the profile is compiled): the device appears in the
        system with a small delay, so it cannot be created on the first keypress — that key would be lost.
        """

    def move_mouse(self, dx: int, dy: int) -> None:  # noqa: B027
        """Relative mouse movement (gyro -> mouse). It goes into the same flush() as the keys."""

    def set_key(self, code: int, down: bool) -> None:  # noqa: B027
        """Presses/releases a keyboard key or mouse button by its evdev code. The event goes into the same flush()."""

