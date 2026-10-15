"""Windows (і резервний варіант для Linux/macOS): читання через бібліотеку `hidapi`.

УВАГА: модуль у Linux-середовищі розробки не запускався (немає пакета `hidapi`
та пристроїв) — на Windows його потрібно перевірити. `pip install hidapi`.

hidapi не віддає файловий дескриптор, тому блокуюче очікування реалізовано як
`read(size, timeout_ms)` із коротким таймаутом: коли звіт є, він повертається
негайно (затримки немає), а таймаут потрібен лише щоб потік-читач міг
перевірити прапорець зупинки.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .base import DeviceGone, DeviceInfo, InputDevice

log = logging.getLogger(__name__)

READ_TIMEOUT_MS = 100


def enumerate_hidapi() -> List[DeviceInfo]:
    import hid
    out = []
    for d in hid.enumerate(0, 0):
        path = d["path"]
        out.append(DeviceInfo(
            path=path.decode() if isinstance(path, bytes) else str(path),
            vid=d["vendor_id"], pid=d["product_id"],
            name=d.get("product_string") or "", interface=d.get("interface_number"),
            backend="hidapi"))
    return out


class HidapiDevice(InputDevice):
    def __init__(self, info: DeviceInfo, read_size: int = 64):
        super().__init__(info, read_size)
        self._dev = None

    def open(self) -> None:
        import hid
        self._dev = hid.device()
        self._dev.open_path(self.info.path.encode())

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    def read(self) -> Optional[bytes]:
        try:
            data = self._dev.read(self.read_size, READ_TIMEOUT_MS)
        except (OSError, ValueError) as e:
            raise DeviceGone(str(e)) from e
        if data is None or (isinstance(data, int) and data < 0):
            raise DeviceGone("hidapi: read error")
        return bytes(data) if data else None   # [] = таймаут

    def wake(self) -> None:
        pass  # потік прокинеться сам за READ_TIMEOUT_MS

    def send_feature(self, data: bytes) -> None:
        if self._dev.send_feature_report(list(data)) < 0:
            raise OSError("send_feature_report: помилка")

    def get_feature(self, report_id: int, length: int) -> bytes:
        return bytes(self._dev.get_feature_report(report_id, length))

    def write_output(self, data: bytes) -> None:
        if self._dev.write(list(data)) < 0:
            raise OSError("write: помилка")
