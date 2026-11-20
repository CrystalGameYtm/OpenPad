"""Linux: wired XInput gamepads read directly over USB (usbfs), without the kernel `xpad` driver.

Why: `xpad` sometimes stops delivering events after a gamepad/receiver is unplugged or re-enumerated, and only
a reboot helps. Here the daemon takes the USB interface itself (USBDEVFS_DISCONNECT_CLAIM detaches `xpad`),
reads the 20-byte input reports from the interrupt IN endpoint and writes rumble reports to the OUT endpoint.
On close the interface is released and the kernel driver is given back.

Scope: the "Xbox 360 wired" protocol — USB interface class 0xFF, subclass 0x5D, protocol 0x01. It is also what
most third-party XInput pads and 2.4 GHz receivers in XInput mode present. Not covered: the wireless Xbox 360
receiver (protocol 0x81) and Xbox One (subclass 0x47); they keep using evdev.

The device state is packed into the same "synthetic report" as for evdev (logic/evdev_layout.py), so the layout,
profiles and the UI behave exactly as with `xpad`. The capabilities are those that `xpad` would report.

Needs read/write access to /dev/bus/usb/BBB/DDD (see packaging/linux/udev/70-gamepad-remapd.rules).
"""
from __future__ import annotations

import ctypes
import errno
import logging
import os
import select
import struct
from typing import Callable, Dict, List, Optional, Tuple

from ..i18n import tr
from ..util import IO, IOR, IOW, IOWR
from .base import DeviceGone, DeviceInfo
from .evdev_linux import EvdevDevice

log = logging.getLogger(__name__)

SYS_USB = "/sys/bus/usb/devices"
DEV_USB = "/dev/bus/usb"

CLASS_VENDOR, SUBCLASS_XINPUT, PROTOCOL_X360 = 0xFF, 0x5D, 0x01

# ---- usbfs ioctls (linux/usbdevice_fs.h) ----
_URB_SIZE = 56 if ctypes.sizeof(ctypes.c_void_p) == 8 else 48


class _DisconnectClaim(ctypes.Structure):
    _fields_ = [("interface", ctypes.c_uint), ("flags", ctypes.c_uint), ("driver", ctypes.c_char * 256)]


class _IoctlArg(ctypes.Structure):
    _fields_ = [("ifno", ctypes.c_int), ("ioctl_code", ctypes.c_int), ("data", ctypes.c_void_p)]


class Urb(ctypes.Structure):
    """struct usbdevfs_urb without the trailing iso_frame_desc[]."""
    _fields_ = [("type", ctypes.c_ubyte), ("endpoint", ctypes.c_ubyte), ("status", ctypes.c_int),
                ("flags", ctypes.c_uint), ("buffer", ctypes.c_void_p), ("buffer_length", ctypes.c_int),
                ("actual_length", ctypes.c_int), ("start_frame", ctypes.c_int), ("stream_id", ctypes.c_uint),
                ("error_count", ctypes.c_int), ("signr", ctypes.c_uint), ("usercontext", ctypes.c_void_p)]


USBDEVFS_URB_TYPE_INTERRUPT = 1
USBDEVFS_SUBMITURB = IOR("U", 10, ctypes.sizeof(Urb))
USBDEVFS_DISCARDURB = IO("U", 11)
USBDEVFS_REAPURBNDELAY = IOW("U", 13, ctypes.sizeof(ctypes.c_void_p))
USBDEVFS_CLAIMINTERFACE = IOR("U", 15, 4)
USBDEVFS_RELEASEINTERFACE = IOR("U", 16, 4)
USBDEVFS_IOCTL = IOWR("U", 18, ctypes.sizeof(_IoctlArg))
USBDEVFS_CONNECT = IO("U", 23)
USBDEVFS_DISCONNECT_CLAIM = IOR("U", 27, ctypes.sizeof(_DisconnectClaim))

_libc = None


def _libc_ioctl():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        _libc.ioctl.restype = ctypes.c_int
    return _libc.ioctl


def _ioctl(fd: int, req: int, arg) -> int:
    rc = _libc_ioctl()(fd, req, arg)
    if rc < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return rc


# ---------------------------------------------------------------------------
# usbfs access layer (replaceable in tests)
# ---------------------------------------------------------------------------
class UsbFs:
    """A thin wrapper over one /dev/bus/usb node: claim, interrupt URBs, release.

    Completed transfers are returned by `reap()` as ``(urb_id, status, data)``; the file descriptor becomes
    "writable" (POLLOUT) for `poll()` when one is ready, and POLLERR/POLLHUP when the device is gone.
    """

    def __init__(self) -> None:
        self.fd = -1
        self._urbs: Dict[int, Tuple[int, Urb, ctypes.Array]] = {}     # address -> (urb_id, urb, buffer)
        self._by_id: Dict[int, int] = {}

    def open(self, path: str) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def fileno(self) -> int:
        return self.fd

    def claim(self, ifnum: int) -> str:
        """Takes the interface, detaching a bound kernel driver. Returns the detached driver name ('' if none)."""
        dc = _DisconnectClaim(ifnum, 0, b"")
        try:
            _ioctl(self.fd, USBDEVFS_DISCONNECT_CLAIM, ctypes.byref(dc))
            return ""
        except OSError as e:
            if e.errno not in (errno.ENOTTY, errno.EINVAL):                # old kernel: fall back to two steps
                raise
        self.connect_detach(ifnum)
        arg = ctypes.c_uint(ifnum)
        _ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, ctypes.byref(arg))
        return ""

    def connect_detach(self, ifnum: int) -> None:
        a = _IoctlArg(ifnum, IO("U", 22), None)                           # USBDEVFS_DISCONNECT
        try:
            _ioctl(self.fd, USBDEVFS_IOCTL, ctypes.byref(a))
        except OSError as e:
            if e.errno != errno.ENODATA:
                raise

    def release(self, ifnum: int) -> None:
        arg = ctypes.c_uint(ifnum)
        _ioctl(self.fd, USBDEVFS_RELEASEINTERFACE, ctypes.byref(arg))

    def connect(self, ifnum: int) -> None:
        """Gives the interface back to the kernel driver."""
        a = _IoctlArg(ifnum, USBDEVFS_CONNECT, None)
        _ioctl(self.fd, USBDEVFS_IOCTL, ctypes.byref(a))

    def submit(self, urb_id: int, endpoint: int, length: int, data: Optional[bytes] = None) -> None:
        buf = (ctypes.c_ubyte * max(1, length))()
        if data:
            ctypes.memmove(buf, data, min(len(data), length))
        urb = Urb()
        urb.type, urb.endpoint = USBDEVFS_URB_TYPE_INTERRUPT, endpoint
        urb.buffer, urb.buffer_length = ctypes.addressof(buf), length
        addr = ctypes.addressof(urb)
        self._urbs[addr] = (urb_id, urb, buf)
        self._by_id[urb_id] = addr
        try:
            _ioctl(self.fd, USBDEVFS_SUBMITURB, ctypes.byref(urb))
        except OSError:
            self._urbs.pop(addr, None)
            self._by_id.pop(urb_id, None)
            raise

    def reap(self) -> Optional[Tuple[int, int, bytes]]:
        ptr = ctypes.c_void_p()
        try:
            _ioctl(self.fd, USBDEVFS_REAPURBNDELAY, ctypes.byref(ptr))
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return None
            if e.errno == errno.ENODEV:
                raise DeviceGone(str(e)) from e
            raise
        item = self._urbs.pop(ptr.value or 0, None)
        if item is None:
            return None
        urb_id, urb, buf = item
        self._by_id.pop(urb_id, None)
        status = urb.status
        return urb_id, status, bytes(buf[:max(0, urb.actual_length)])

    def discard(self, urb_id: int) -> None:
        addr = self._by_id.get(urb_id)
        item = self._urbs.get(addr) if addr is not None else None
        if item:
            try:
                _ioctl(self.fd, USBDEVFS_DISCARDURB, ctypes.byref(item[1]))
            except OSError:
                pass

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = -1


# ---------------------------------------------------------------------------
# Enumeration (sysfs only: nothing is opened here)
# ---------------------------------------------------------------------------
def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _hex(path: str) -> Optional[int]:
    t = _read(path)
    try:
        return int(t, 16) if t else None
    except ValueError:
        return None


def _int(path: str) -> Optional[int]:
    t = _read(path)
    try:
        return int(t) if t else None
    except ValueError:
        return None


# What xpad reports for an Xbox 360 pad: the layout is built from this (identical to the evdev path).
BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR, BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x136, 0x137, 0x13A, 0x13B, 0x13C, 0x13D, 0x13E
XPAD_KEYS = [BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST, BTN_TL, BTN_TR, BTN_SELECT, BTN_START, BTN_MODE,
             BTN_THUMBL, BTN_THUMBR]
XPAD_ABS = {0x00: (-32768, 32767), 0x01: (-32768, 32767), 0x02: (0, 255), 0x03: (-32768, 32767),
            0x04: (-32768, 32767), 0x05: (0, 255), 0x10: (-1, 1), 0x11: (-1, 1)}


def enumerate_xusb(sys_root: str = SYS_USB, dev_root: str = DEV_USB, *,
                   access: Callable[[str, int], bool] = os.access) -> List[DeviceInfo]:
    """XInput (Xbox 360 wired protocol) interfaces from /sys/bus/usb/devices.

    A device is listed only when its /dev/bus/usb node is readable and writable by us: otherwise the evdev
    path stays in use (see `enumerate_devices`).
    """
    out: List[DeviceInfo] = []
    try:
        names = sorted(os.listdir(sys_root))
    except OSError:
        return out
    for name in names:
        if ":" not in name:                                         # interfaces look like "1-2:1.0"
            continue
        idir = os.path.join(sys_root, name)
        if (_hex(os.path.join(idir, "bInterfaceClass")), _hex(os.path.join(idir, "bInterfaceSubClass")),
                _hex(os.path.join(idir, "bInterfaceProtocol"))) != (CLASS_VENDOR, SUBCLASS_XINPUT, PROTOCOL_X360):
            continue
        ifnum = _hex(os.path.join(idir, "bInterfaceNumber"))
        ddir = os.path.join(sys_root, name.split(":")[0])
        vid, pid = _hex(os.path.join(ddir, "idVendor")), _hex(os.path.join(ddir, "idProduct"))
        bus, dev = _int(os.path.join(ddir, "busnum")), _int(os.path.join(ddir, "devnum"))
        if None in (ifnum, vid, pid, bus, dev):
            continue
        ep_in = ep_out = None
        in_size = 0
        for ep in sorted(os.listdir(idir)):
            if not ep.startswith("ep_"):
                continue
            addr = _hex(os.path.join(idir, ep, "bEndpointAddress"))
            attrs = _hex(os.path.join(idir, ep, "bmAttributes"))
            if addr is None or attrs is None or attrs & 3 != 3:      # interrupt endpoints only
                continue
            if addr & 0x80 and ep_in is None:
                ep_in = addr
                in_size = _hex(os.path.join(idir, ep, "wMaxPacketSize")) or 0
            elif not addr & 0x80 and ep_out is None:
                ep_out = addr
        if ep_in is None:
            continue
        node = os.path.join(dev_root, f"{bus:03d}", f"{dev:03d}")
        if not access(node, os.R_OK | os.W_OK):
            log.debug("xusb: no access to %s (udev rule?); the evdev path is used", node)
            continue
        label = " ".join(x for x in (_read(os.path.join(ddir, "manufacturer")), _read(os.path.join(ddir, "product"))) if x)
        out.append(DeviceInfo(
            path=node, vid=vid, pid=pid, name=label or "XInput gamepad", interface=ifnum, bus=3, backend="xusb",
            extra={"keys": list(XPAD_KEYS), "abs": dict(XPAD_ABS), "range_known": True,
                   "usb": {"ifnum": ifnum, "ep_in": ep_in, "in_size": in_size, "ep_out": ep_out, "sysfs": os.path.realpath(idir)}}))
    return out


def evdev_node_of(info: DeviceInfo, xusb_infos: List[DeviceInfo]) -> bool:
    """True if the evdev `info` is the xpad node of one of the interfaces in `xusb_infos` (the same gamepad)."""
    sysfs = (info.extra or {}).get("sysfs")
    if not sysfs:
        return False
    return any(sysfs.startswith(x.extra["usb"]["sysfs"] + os.sep) for x in xusb_infos)


def reattach_kernel_drivers(sys_root: str = SYS_USB, dev_root: str = DEV_USB) -> int:
    """After a crash a claimed interface may stay without a driver: give it back to the kernel. Returns the count."""
    n = 0
    for info in enumerate_xusb(sys_root, dev_root):
        if os.path.exists(os.path.join(info.extra["usb"]["sysfs"], "driver")):
            continue
        fs = UsbFs()
        try:
            fs.open(info.path)
            fs.connect(info.extra["usb"]["ifnum"])
            n += 1
        except OSError as e:
            log.debug("xusb: could not reattach %s: %s", info.path, e)
        finally:
            fs.close()
    return n


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
IN_BUF = 64                    # URB buffer: the endpoint may deliver more than 20 bytes (smaller => EOVERFLOW), as in xpad
IN_LEN = 20                    # input report: 00 14 <buttons x2> <LT> <RT> <LX LY RX RY s16le> <6 zero bytes>
_TRIGGER_MAX = 255


def decode_report(data: bytes) -> Optional[Tuple[int, Dict[int, int], Tuple[int, int]]]:
    """-> (set of pressed evdev button codes, {ABS code: value}, (hat x, hat y)).

    Returns None for anything that is not an input report (other message types are ignored).
    """
    if len(data) < IN_LEN or data[0] != 0x00 or data[1] != 0x14:
        return None
    b2, b3 = data[2], data[3]
    keys = set()
    for bit, code in ((4, BTN_START), (5, BTN_SELECT), (6, BTN_THUMBL), (7, BTN_THUMBR)):
        if b2 >> bit & 1:
            keys.add(code)
    for bit, code in ((0, BTN_TL), (1, BTN_TR), (2, BTN_MODE), (4, BTN_SOUTH), (5, BTN_EAST),
                      (6, BTN_NORTH), (7, BTN_WEST)):
        if b3 >> bit & 1:
            keys.add(code)
    lx, ly, rx, ry = struct.unpack_from("<hhhh", data, 6)
    abs_ = {0x00: lx, 0x01: ~ly, 0x02: data[4], 0x03: rx, 0x04: ~ry, 0x05: data[5]}     # Y: up is minus, as in xpad
    hat = ((b2 >> 3 & 1) - (b2 >> 2 & 1), (b2 >> 1 & 1) - (b2 & 1))
    return keys, abs_, hat


def rumble_report(strong16: int, weak16: int) -> bytes:
    """Output report for the Xbox 360 wired protocol: big (left) motor, small (right) motor, 8 bits each."""
    return bytes((0x00, 0x08, 0x00, (strong16 >> 8) & 0xFF, (weak16 >> 8) & 0xFF, 0x00, 0x00, 0x00))


_IN_ID, _OUT_ID = 1, 2


class XusbDevice(EvdevDevice):
    """Reads the gamepad over USB and returns the same synthetic reports as `EvdevDevice`."""

    def __init__(self, info: DeviceInfo, read_size: int = 64, *, fs_factory: Callable[[], UsbFs] = UsbFs):
        super().__init__(info, read_size)
        u = (info.extra or {}).get("usb") or {}
        self._ifnum: int = u.get("ifnum", 0)
        self._ep_in: int = u.get("ep_in", 0x81)
        self._ep_out: Optional[int] = u.get("ep_out")
        self._in_buf = max(IN_BUF, int(u.get("in_size") or 0))    # never smaller than the endpoint packet size
        self._fs_factory = fs_factory
        self._fs: Optional[UsbFs] = None
        self._claimed = False
        self._out_pending = False
        self._failures = 0

    # ---- lifecycle ----
    def open(self) -> None:
        fs = self._fs_factory()
        try:
            fs.open(self.info.path)
        except PermissionError as e:
            raise PermissionError(e.errno, tr('no access to {0}: install the udev rules (packaging/linux/udev/70-gamepad-remapd.rules) '
                                              'or run with sudo', self.info.path)) from e
        self._fs = fs
        try:
            fs.claim(self._ifnum)
            self._claimed = True
            self._wake_r, self._wake_w = os.pipe()
            self._poll = select.poll()
            self._poll.register(fs.fileno(), select.POLLOUT | select.POLLERR | select.POLLHUP)
            self._poll.register(self._wake_r, select.POLLIN)
            fs.submit(_IN_ID, self._ep_in, self._in_buf)
        except BaseException:
            self.close()
            raise
        self.rumble_ok = self._ep_out is not None
        self._ready.append(self._pack())
        log.info(tr('%s: XInput gamepad read directly over USB (the kernel driver is detached while the daemon runs)'), self.info.path)

    def close(self) -> None:
        fs, self._fs = self._fs, None
        if fs is not None:
            if self._claimed:
                self._rumble_stop(fs)
                fs.discard(_IN_ID)
                try:
                    fs.release(self._ifnum)
                except OSError:
                    pass
                try:
                    fs.connect(self._ifnum)                 # give the interface back to xpad
                except OSError as e:
                    log.debug("xusb: could not reattach the kernel driver: %s", e)
            self._claimed = False
            fs.close()
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1

    def grab_siblings(self) -> int:
        return 0                                            # no kernel nodes exist while the interface is ours

    # ---- hot read ----
    def read(self) -> Optional[bytes]:
        if self._ready:
            return self._ready.popleft()
        fs = self._fs
        if fs is None:
            raise DeviceGone("xusb: closed")
        for fd, mask in self._poll.poll():
            if fd == self._wake_r:
                os.read(self._wake_r, 64)
                return None
            if mask & (select.POLLERR | select.POLLHUP):
                raise DeviceGone(tr('USB: the device was disconnected'))
            if mask & select.POLLOUT:
                self._drain(fs)
                return self._ready.popleft() if self._ready else None
        return None

    def _drain(self, fs: UsbFs) -> None:
        while True:
            done = fs.reap()
            if done is None:
                return
            urb_id, status, data = done
            if urb_id == _OUT_ID:
                self._out_pending = False
                if status != 0:
                    log.warning('xusb: %s: rumble transfer failed: %s (status %d, ep 0x%02x)',
                                self.info.path, os.strerror(-status), status, self._ep_out or 0)
                else:
                    log.debug('xusb: %s: rumble report delivered', self.info.path)
                nxt, self._pending_rumble = self._pending_rumble, None
                if nxt is not None:                         # an update arrived while the report was in flight
                    try:
                        self.set_rumble(*nxt)
                    except OSError as e:
                        log.warning(tr('%s: rumble not delivered: %s'), self.info.path, e)
                continue
            if urb_id == _OUT_ID + 1:
                continue
            if status == 0:
                self._failures = 0
                dec = decode_report(data)
                if dec is not None:
                    self._apply(*dec)
            elif status in (-errno.ENODEV, -errno.ESHUTDOWN, -errno.ENOENT, -errno.EPROTO):
                raise DeviceGone(f"USB transfer: {os.strerror(-status)}")
            else:                                           # EPIPE (stall), EOVERFLOW ...: retry a few times
                self._failures += 1
                if self._failures == 1:
                    log.warning('xusb: %s: IN transfer failed: %s (status %d, ep 0x%02x, buffer %d bytes, got %d bytes)',
                                self.info.path, os.strerror(-status), status, self._ep_in, self._in_buf, len(data))
                if self._failures > 20:
                    raise DeviceGone(f"USB transfer: {os.strerror(-status)}")
            try:
                fs.submit(_IN_ID, self._ep_in, self._in_buf)
            except OSError as e:
                raise DeviceGone(str(e)) from e

    def _apply(self, keys: set, abs_: Dict[int, int], hat: Tuple[int, int]) -> None:
        bits = 0
        for code in keys:
            i = self._index.get(code)
            if i is not None:
                bits |= 1 << i
        self._keys = bits
        self._abs.update(abs_)
        self._hat[0], self._hat[1] = hat
        self._ready.append(self._pack())

    # ---- rumble ----
    def set_rumble(self, strong16: int, weak16: int) -> None:
        fs = self._fs
        if fs is None or self._ep_out is None:
            raise OSError(errno.ENOTSUP, tr('USB XInput: rumble is unavailable (no OUT endpoint)'))
        if self._out_pending:                               # the previous report is still in flight: skip this update
            self._pending_rumble = (strong16, weak16)
            return
        log.debug('xusb: %s: rumble strong=%04x weak=%04x -> ep 0x%02x', self.info.path, strong16, weak16, self._ep_out)
        self._out_pending = True
        try:
            fs.submit(_OUT_ID, self._ep_out, 8, rumble_report(strong16, weak16))
        except OSError:
            self._out_pending = False
            raise

    _pending_rumble: Optional[Tuple[int, int]] = None

    def _rumble_stop(self, fs: UsbFs) -> None:
        """Best effort on close: send "motors off" and give the transfer a moment to complete."""
        if self._ep_out is None:
            return
        try:
            fs.submit(_OUT_ID + 1, self._ep_out, 8, rumble_report(0, 0))
            select.select([], [fs.fileno()], [], 0.05)
            fs.reap()
        except (OSError, DeviceGone, ValueError):
            pass

    def write_output(self, data: bytes) -> None:
        raise OSError(errno.ENOTSUP, tr('USB XInput: output reports are not supported'))
