"""Абстракція фізичного HID-пристрою (Input Layer) та виконання handshake."""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..logic.layout import HandshakeStep
from ..util import hex_str

log = logging.getLogger(__name__)


class DeviceGone(Exception):
    """Пристрій від'єднано (або read() більше не можливий)."""


@dataclass(frozen=True)
class DeviceInfo:
    path: str                      # "/dev/hidraw3" або hidapi-шлях
    vid: int
    pid: int
    name: str = ""
    interface: Optional[int] = None
    bus: Optional[int] = None      # 3 = USB, 5 = Bluetooth (лише Linux)
    backend: str = ""
    # Сирий HID Report Descriptor (Linux: sysfs). Потрібен для автогенерації розкладки.
    # Не бере участі в порівнянні/хешуванні й не потрапляє в as_dict() (великий і не для UI).
    descriptor: Optional[bytes] = field(default=None, repr=False, compare=False)
    # evdev-пристрої: можливості {"keys": [коди], "abs": {код: (min, max)}, "range_known": bool}
    # (звідси складається розкладка; для hidraw/hidapi — None).
    extra: Optional[dict] = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict:
        return {"path": self.path, "vid": f"{self.vid:04x}", "pid": f"{self.pid:04x}",
                "name": self.name, "interface": self.interface, "bus": self.bus,
                "backend": self.backend}


class InputDevice(ABC):
    """Контракт: `read()` блокується до появи звіту — це і є «блокуюче читання»."""

    def __init__(self, info: DeviceInfo, read_size: int = 64):
        self.info = info
        self.read_size = read_size

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> Optional[bytes]:
        """Блокуюче читання одного сирого звіту (з Report ID, якщо пристрій його
        використовує). None — читання перервано `wake()`/таймаутом, потрібно
        перевірити прапорець зупинки. DeviceGone — пристрій зник."""

    @abstractmethod
    def wake(self) -> None:
        """Переривання блокуючого read() з іншого потоку (для коректної зупинки)."""

    @abstractmethod
    def send_feature(self, data: bytes) -> None:
        """Feature-звіт (SET_REPORT/Feature). data[0] — Report ID (0, якщо немає нумерації)."""

    @abstractmethod
    def get_feature(self, report_id: int, length: int) -> bytes: ...

    @abstractmethod
    def write_output(self, data: bytes) -> None:
        """Output-звіт (вібрація, LED, vendor-команди). data[0] — Report ID."""

    def grab_siblings(self) -> int:  # noqa: B027
        """Ексклюзивний захоплення системних evdev-вузлів цього ж пристрою
        (щоб ігри не бачили і фізичний, і віртуальний геймпад). Повертає кількість."""
        return 0


def run_handshake(dev: InputDevice, steps: Iterable[HandshakeStep]) -> None:
    """Виконує послідовність активації vendor-режиму ресивера.

    Помилка окремого кроку не фатальна: деякі пристрої відповідають помилкою на
    feature_get, але все одно переходять у потрібний режим. Усе логується, щоб
    було видно, який крок спрацював.
    """
    for i, st in enumerate(steps):
        try:
            if st.op == "feature_set":
                dev.send_feature(st.data)
                log.info("handshake[%d] feature_set %s", i, hex_str(st.data))
            elif st.op == "output":
                dev.write_output(st.data)
                log.info("handshake[%d] output %s", i, hex_str(st.data))
            elif st.op == "feature_get":
                resp = dev.get_feature(st.report_id, st.length)
                log.info("handshake[%d] feature_get id=%d -> %s", i, st.report_id, hex_str(resp))
            elif st.op == "sleep_ms":
                time.sleep(st.ms / 1000.0)
        except Exception as e:  # noqa: BLE001
            log.warning("handshake[%d] %s: помилка: %s", i, st.op, e)
