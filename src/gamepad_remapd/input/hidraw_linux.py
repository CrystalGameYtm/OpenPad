"""Linux: пряме читання сирих HID-звітів із /dev/hidrawN (stdlib: os, fcntl, select).

Чому hidraw без бібліотек: `os.read()` на дескрипторі hidraw — це один системний
виклик, що повертає рівно один звіт без проміжних шарів (libusb/hidapi додають
копіювання, потоки та черги). Для feature-звітів використовуються ті самі ioctl,
що й у hidapi (HIDIOCSFEATURE / HIDIOCGFEATURE).
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

log = logging.getLogger(__name__)

SYS_HIDRAW = "/sys/class/hidraw"
EVIOCGRAB = IOW("E", 0x90, 4)   # ексклюзивне захоплення evdev-вузла


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
    """Перелік hidraw-пристроїв зі sysfs (без відкриття вузлів, права не потрібні)."""
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
        # Номер USB-інтерфейсу: файл bInterfaceNumber лежить в батьківському каталозі HID-пристрою.
        iface: Optional[int] = None
        try:
            txt = _read(os.path.join(os.path.dirname(os.path.realpath(dev_dir)), "bInterfaceNumber"))
            iface = int(txt, 16) if txt else None
        except (ValueError, OSError):
            pass
        # Report descriptor — бінарний файл у sysfs, читається без прав root. Якщо його немає
        # (віртуальні/старі ядра) — просто None: авторозкладка для цього пристрою недоступна.
        try:
            with open(os.path.join(dev_dir, "report_descriptor"), "rb") as f:
                descriptor: Optional[bytes] = f.read() or None
        except OSError:
            descriptor = None
        out.append(DeviceInfo(path=f"/dev/{name}", vid=vid, pid=pid, name=props.get("HID_NAME", ""),
                              interface=iface, bus=bus, backend="hidraw", descriptor=descriptor))
    return out


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
            # O_RDWR потрібен для feature/output-звітів; якщо прав на запис немає — лише читання.
            self._fd = os.open(self.info.path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError:
            log.warning("%s: немає прав на запис — вібрація/handshake недоступні "
                        "(див. udev-правила)", self.info.path)
            self._fd = os.open(self.info.path, os.O_RDONLY | os.O_CLOEXEC)
            self.can_write = False
        # Канал для пробудження блокуючого poll() при зупинці потоку-читача.
        self._wake_r, self._wake_w = os.pipe()
        self._poll = select.poll()
        self._poll.register(self._fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        self._poll.register(self._wake_r, select.POLLIN)

    def close(self) -> None:
        for fd in self._grabbed:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)      # відпускаємо захоплення
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

    # ---- ГАРЯЧЕ ЧИТАННЯ ------------------------------------------------------
    def read(self) -> Optional[bytes]:
        # poll() блокує потік у ядрі (GIL відпущено) до появи звіту, зупинки чи відключення.
        for fd, mask in self._poll.poll():
            if fd == self._fd:
                if mask & select.POLLIN:
                    try:
                        return os.read(fd, self.read_size)   # рівно один HID-звіт
                    except OSError as e:
                        raise DeviceGone(str(e)) from e
                raise DeviceGone("hidraw: POLLERR/POLLHUP (пристрій відключено)")
            os.read(self._wake_r, 64)                         # пробудження через wake()
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
        # HIDIOCSFEATURE: перший байт буфера — Report ID.
        fcntl.ioctl(self._fd, HIDIOCSFEATURE(len(data)), bytes(data))

    def get_feature(self, report_id: int, length: int) -> bytes:
        buf = bytearray(length)
        buf[0] = report_id                         # ядро читає Report ID з першого байта
        n = fcntl.ioctl(self._fd, HIDIOCGFEATURE(length), buf, True)
        return bytes(buf[:n if isinstance(n, int) and n > 0 else length])

    def write_output(self, data: bytes) -> None:
        os.write(self._fd, data)                  # звичайний write() = Output report

    # ---- приховування «дубля» -------------------------------------------------
    def grab_siblings(self) -> int:
        """EVIOCGRAB на evdev-вузли, створені системним драйвером для цього ж пристрою."""
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
                log.warning("не вдалося захопити %s: %s", node, e)
        return count
