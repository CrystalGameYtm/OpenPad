"""Abstraction of a physical HID device (Input Layer) and execution of the handshake."""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..logic.layout import HandshakeStep
from ..util import hex_str
from ..i18n import tr

log = logging.getLogger(__name__)


class BackendUnavailable(RuntimeError):
    """The read backend cannot be used (missing library/driver); the text is a ready-made hint for the user."""


class DeviceGone(Exception):
    """The device was disconnected (or read() is no longer possible)."""


@dataclass(frozen=True)
class DeviceInfo:
    path: str                      # "/dev/hidraw3" or a hidapi path
    vid: int
    pid: int
    name: str = ""
    interface: Optional[int] = None
    bus: Optional[int] = None      # 3 = USB, 5 = Bluetooth (Linux only)
    backend: str = ""
    
    
    descriptor: Optional[bytes] = field(default=None, repr=False, compare=False)
    # evdev devices: capabilities {"keys": [codes], "abs": {code: (min, max)}, "range_known": bool}
    # (the layout is built from this; None for hidraw/hidapi).
    extra: Optional[dict] = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict:
        return {"path": self.path, "vid": f"{self.vid:04x}", "pid": f"{self.pid:04x}",
                "name": self.name, "interface": self.interface, "bus": self.bus,
                "backend": self.backend}


def evdev_format(info) -> bool:
    """True for devices whose reports are the synthetic evdev-format reports (kernel evdev nodes and direct USB XInput)."""
    return getattr(info, "backend", "") in ("evdev", "xusb")


class InputDevice(ABC):
    """Contract: `read()` blocks until a report arrives — this is the "blocking read"."""

    def __init__(self, info: DeviceInfo, read_size: int = 64):
        self.info = info
        self.read_size = read_size

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> Optional[bytes]:
        """Blocking read of one raw report (including the Report ID if the device uses one).
            None — the read was interrupted by `wake()`/a timeout; the stop flag must be
            checked. DeviceGone — the device disappeared.
        """

    @abstractmethod
    def wake(self) -> None:
        """Interrupts a blocking read() from another thread (for a clean stop)."""

    @abstractmethod
    def send_feature(self, data: bytes) -> None:
        """Feature report (SET_REPORT/Feature). data[0] — the Report ID (0 if there is no numbering)."""

    @abstractmethod
    def get_feature(self, report_id: int, length: int) -> bytes: ...

    @abstractmethod
    def write_output(self, data: bytes) -> None:
        """Output report (rumble, LED, vendor commands). data[0] — the Report ID."""

    def grab_siblings(self) -> int:  # noqa: B027
        """Exclusively grabs the system evdev nodes of the same device
        (so games do not see both the physical and the virtual gamepad). Returns the count.
        """
        return 0


def run_handshake(dev: InputDevice, steps: Iterable[HandshakeStep]) -> None:
    """Runs the sequence that activates the receiver's vendor mode.

        An error in a single step is not fatal: some devices answer with an error to
        feature_get but still switch to the required mode. Everything is logged so it is
        visible which step worked.
    """
    bus = getattr(getattr(dev, "info", None), "bus", None)
    for i, st in enumerate(steps):
        if st.bus is not None and bus is not None and st.bus != bus:
            continue                                    
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
            log.warning(tr('handshake[%d] %s: error: %s'), i, st.op, e)


def is_streaming(dev: InputDevice, accepts, window: float = 0.3) -> bool:
    """Return True if the device already sends state reports that `accepts` recognises.

    Reads (and discards) reports for up to `window` seconds; a timer wakes the blocking read.
    Used to skip the vendor handshake when the device is already in the wanted mode.
    """
    timer = threading.Timer(window, dev.wake)
    timer.daemon = True
    timer.start()
    deadline = time.monotonic() + window
    try:
        while time.monotonic() < deadline:
            raw = dev.read()
            if raw is None:
                break
            if accepts(raw):
                return True
    except DeviceGone:
        raise
    except Exception as e:  # noqa: BLE001
        log.debug("streaming probe failed: %s", e)
    finally:
        timer.cancel()
    return False
