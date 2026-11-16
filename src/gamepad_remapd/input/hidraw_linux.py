"""Linux: direct reading of raw HID reports from /dev/hidrawN (stdlib: os, fcntl, select).

Why hidraw without libraries: `os.read()` on a hidraw descriptor is a single system call that returns exactly one
report without intermediate layers (libusb/hidapi add copies, threads and queues). Feature reports use the same
ioctls as hidapi (HIDIOCSFEATURE / HIDIOCGFEATURE).
"""
from __future__ import annotations

import glob
import logging
import os
import re
import select
import fcntl
from typing import List, Optional

from ..util import IOC, IOR, IOW, IOC_READ, IOC_WRITE
from .base import DeviceGone, DeviceInfo, InputDevice
from ..i18n import tr

log = logging.getLogger(__name__)

SYS_HIDRAW = "/sys/class/hidraw"
EVIOCGRAB = IOW("E", 0x90, 4)   


def HIDIOCSFEATURE(length: int) -> int:
    return IOC(IOC_WRITE | IOC_READ, "H", 0x06, length)


def HIDIOCGFEATURE(length: int) -> int:
    return IOC(IOC_WRITE | IOC_READ, "H", 0x07, length)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def enumerate_hidraw(sys_root: str = SYS_HIDRAW) -> List[DeviceInfo]:
    """List of hidraw devices from sysfs (without opening the nodes; no permissions needed)."""
    def natural(n: str) -> int:
        m = re.search(r"(\d+)$", n)
        return int(m.group(1)) if m else 0

    out: List[DeviceInfo] = []
    try:
        names = sorted(os.listdir(sys_root), key=natural)
    except OSError:
        return out
    for name in names:
        dev_dir = os.path.join(sys_root, name, "device")
        props = dict(line.split("=", 1) for line in _read(os.path.join(dev_dir, "uevent")).splitlines()
                     if "=" in line)
        hid_id = props.get("HID_ID")
        if not hid_id:
            continue
        try:
            bus, vid, pid = (int(x, 16) for x in hid_id.split(":"))
        except ValueError:
            continue
        
        iface: Optional[int] = None
        try:
            txt = _read(os.path.join(os.path.dirname(os.path.realpath(dev_dir)), "bInterfaceNumber"))
            iface = int(txt, 16) if txt else None
        except (ValueError, OSError):
            pass
        # The report descriptor is a binary file in sysfs, readable without root. If it is missing
        # (virtual/old kernels) it is simply None: auto-layout is unavailable for this device.
        try:
            with open(os.path.join(dev_dir, "report_descriptor"), "rb") as f:
                descriptor: Optional[bytes] = f.read() or None
        except OSError:
            descriptor = None
        out.append(DeviceInfo(path=f"/dev/{name}", vid=vid, pid=pid, name=props.get("HID_NAME", ""),
                              interface=iface, bus=bus, backend="hidraw", descriptor=descriptor))
    return out


_WARNED_RO: set = set()


HIDE_GROUP = os.environ.get("GAMEPAD_REMAPD_GROUP", "gamepad-remapd")


def _closed_to_group(st: os.stat_result, group: str = HIDE_GROUP) -> bool:
    """The node belongs to the hiding group and is closed to "others" — it has already been hidden by the rule 71-…hide-hidraw."""
    try:
        import grp
        gid = grp.getgrnam(group).gr_gid
    except (ImportError, KeyError):
        return False
    return st.st_gid == gid and not (st.st_mode & 0o007)


class HidrawDevice(InputDevice):
    def __init__(self, info: DeviceInfo, read_size: int = 64, sys_root: str = SYS_HIDRAW):
        super().__init__(info, read_size)
        self._sys_root = sys_root
        self._fd = -1
        self._wake_r = self._wake_w = -1
        self._poll: Optional[select.poll] = None
        self._grabbed: List[int] = []
        self.can_write = True

    def open(self) -> None:
        try:
            # O_RDWR is needed for feature/output reports; without write permission — read only.
            self._fd = os.open(self.info.path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError:
            if self.info.path not in _WARNED_RO:             
                _WARNED_RO.add(self.info.path)
                log.warning(tr('%s: no write permission — rumble/handshake are unavailable (see the udev rules)'), self.info.path)
            self._fd = os.open(self.info.path, os.O_RDONLY | os.O_CLOEXEC)
            self.can_write = False
        
        self._wake_r, self._wake_w = os.pipe()
        self._poll = select.poll()
        self._poll.register(self._fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        self._poll.register(self._wake_r, select.POLLIN)

    def close(self) -> None:
        for fd in self._grabbed:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)      
            except OSError:
                pass
            os.close(fd)
        self._grabbed.clear()
        for fd in (self._fd, self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._fd = self._wake_r = self._wake_w = -1

    # ---- HOT READ ----
    def read(self) -> Optional[bytes]:
        # poll() blocks the thread in the kernel (GIL released) until a report, a stop or a disconnect.
        for fd, mask in self._poll.poll():
            if fd == self._fd:
                if mask & select.POLLIN:
                    try:
                        return os.read(fd, self.read_size)   
                    except OSError as e:
                        raise DeviceGone(str(e)) from e
                raise DeviceGone(tr('hidraw: POLLERR/POLLHUP (device disconnected)'))
            os.read(self._wake_r, 64)                         
            return None
        return None

    def wake(self) -> None:
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    # ---- feature / output ----------------------------------------------------
    def send_feature(self, data: bytes) -> None:
        # HIDIOCSFEATURE: the first byte of the buffer is the Report ID.
        fcntl.ioctl(self._fd, HIDIOCSFEATURE(len(data)), bytes(data))

    def get_feature(self, report_id: int, length: int) -> bytes:
        buf = bytearray(length)
        buf[0] = report_id                         
        n = fcntl.ioctl(self._fd, HIDIOCGFEATURE(length), buf, True)
        return bytes(buf[:n if isinstance(n, int) and n > 0 else length])

    def write_output(self, data: bytes) -> None:
        os.write(self._fd, data)                  

    # ---- hiding the "duplicate" ----
    def grab_siblings(self) -> int:
        """EVIOCGRAB on the evdev nodes created by the system driver for this same device."""
        name = os.path.basename(self.info.path)
        pattern = os.path.join(self._sys_root, name, "device", "input", "input*", "event*")
        count = 0
        for sysdir in glob.glob(pattern):
            node = "/dev/input/" + os.path.basename(sysdir)
            try:
                fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                fcntl.ioctl(fd, EVIOCGRAB, 1)
                self._grabbed.append(fd)
                count += 1
            except OSError as e:
                log.warning(tr('could not grab %s: %s'), node, e)
        return count
