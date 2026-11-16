"""Windows (and the fallback for Linux/macOS): reading through the `hidapi` library.

WARNING: this module has not been run in the Linux development environment (no `hidapi` package
and no devices) — it must be verified on Windows. `pip install hidapi`.

hidapi does not expose a file descriptor, so the blocking wait is implemented as
`read(size, timeout_ms)` with a short timeout: when a report is there, it returns
immediately (no added latency); the timeout is only needed so the reader thread can
check the stop flag.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .base import BackendUnavailable, DeviceGone, DeviceInfo, InputDevice
from ..i18n import tr

log = logging.getLogger(__name__)

READ_TIMEOUT_MS = 100
_BUS = {1: 3, 2: 5}                 # hid_bus_type from hidapi -> bus in our numbering (3 = USB, 5 = Bluetooth)


def _import_hid():
    try:
        import hid
    except ImportError as e:
        raise BackendUnavailable(
            tr('the hidapi library (module `hid`) is missing: pip install hidapi  (or pip install ".[windows]"). If the build failed on a new Python version (no ready wheel), use Python 3.12/3.13 or install the Visual C++ Build Tools. [{0}]', e)) from e
    return hid


def enumerate_hidapi() -> List[DeviceInfo]:
    hid = _import_hid()
    out = []
    for d in hid.enumerate(0, 0):
        path = d["path"]
        out.append(DeviceInfo(
            path=path.decode() if isinstance(path, bytes) else str(path),
            vid=d["vendor_id"], pid=d["product_id"],
            name=d.get("product_string") or "",
            interface=(d.get("interface_number") if d.get("interface_number", -1) != -1 else None),
            bus=_BUS.get(d.get("bus_type")),      # hidapi >= 0.13: 1 = USB, 2 = Bluetooth; otherwise None
            backend="hidapi",
            extra={"usage_page": d.get("usage_page"), "usage": d.get("usage")}))
    return out


class HidapiDevice(InputDevice):
    def __init__(self, info: DeviceInfo, read_size: int = 64):
        super().__init__(info, read_size)
        self._dev = None

    def open(self) -> None:
        hid = _import_hid()
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
        return bytes(data) if data else None   # [] = timeout

    def wake(self) -> None:
        pass  

    def send_feature(self, data: bytes) -> None:
        if self._dev.send_feature_report(list(data)) < 0:
            raise OSError(tr('send_feature_report: error'))

    def get_feature(self, report_id: int, length: int) -> bytes:
        return bytes(self._dev.get_feature_report(report_id, length))

    def write_output(self, data: bytes) -> None:
        if self._dev.write(list(data)) < 0:
            raise OSError(tr('write: error'))
