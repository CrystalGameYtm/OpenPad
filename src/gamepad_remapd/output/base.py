"""Абстракція віртуального геймпада (Output Layer).

Pipeline працює лише з цим інтерфейсом, тому однаковий код логіки обслуговує
і Linux (uinput), і Windows (ViGEm).

Контракт швидкодії: `set_button` / `set_axis` лише ЗАПИСУЮТЬ зміну у внутрішній
буфер; єдиний системний виклик (write у /dev/uinput або IOCTL ViGEm) робиться в
`flush()` — один раз за вхідний звіт. Для Linux це означає, що всі зміни одного
звіту й завершальний SYN_REPORT потрапляють до ядра ОДНИМ `write()` -> споживач
(гра) бачить цілісний кадр стану.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class OutputBackend(ABC):
    kind: str = "abstract"
    #: Кількість «звичайних» кнопок-ідентифікаторів (id 0..n_buttons-1).
    n_buttons: int = 0
    #: Викликається, коли гра просить увімкнути/вимкнути вібрацію: (strong16, weak16).
    #: Може викликатись з event-loop (Linux) або зі стороннього потоку (Windows).
    on_rumble: Optional[Callable[[int, int], None]] = None

    @abstractmethod
    def open(self) -> None:
        """Створює віртуальний пристрій."""

    @abstractmethod
    def close(self) -> None:
        """Знищує віртуальний пристрій."""

    @abstractmethod
    def button_id(self, name: str) -> Optional[int]:
        """Логічне ім'я кнопки ("A", "M1", "UP"...) -> числовий id або None,
        якщо цей backend не вміє віддавати таку кнопку."""

    @abstractmethod
    def axis_id(self, name: str) -> int:
        """"LX" | "LY" | "RX" | "RY" | "LT" | "RT" -> числовий id осі."""

    @abstractmethod
    def set_button(self, bid: int, down: bool) -> None: ...

    @abstractmethod
    def set_axis(self, aid: int, value: int) -> None:
        """value у канонічних одиницях: стіки -32768..32767 (вгору = мінус,
        як в evdev), тригери 0..255."""

    @abstractmethod
    def flush(self) -> None:
        """Віддає накопичені зміни споживачу (один системний виклик)."""

    @abstractmethod
    def release_all(self) -> None:
        """Нейтральний стан: усі кнопки відпущені, осі в центрі."""

    def attach_loop(self, loop) -> None:  # noqa: B027 - необов'язковий хук
        """Підключає backend до asyncio-циклу (для читання FF-запитів)."""
