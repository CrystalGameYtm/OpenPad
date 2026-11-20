"""Direct USB XInput backend: report decoding, sysfs listing, claim/release, rumble, and integration with the layout."""
from __future__ import annotations

import errno
import os
import struct
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional, Tuple

from gamepad_remapd.input import DeviceGone, enumerate_devices, evdev_format
from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.input.xusb_linux import (BTN_NORTH, BTN_SOUTH, BTN_START, IN_BUF, IN_LEN, XusbDevice, decode_report,
                                              enumerate_xusb, evdev_node_of, rumble_report)
from gamepad_remapd.logic.autolayout import try_layout
from gamepad_remapd.logic.layout import ReportParser
from gamepad_remapd.core.session import device_id
from gamepad_remapd.logic.protocol import describe_protocol, render_protocol


def x360_report(*, b2=0, b3=0, lt=0, rt=0, lx=0, ly=0, rx=0, ry=0) -> bytes:
    return bytes((0x00, 0x14, b2, b3, lt, rt)) + struct.pack("<hhhh", lx, ly, rx, ry) + bytes(6)


class FakeFs:
    """Stands in for UsbFs: records calls, returns queued completions."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.done: List[Tuple[int, int, bytes]] = []
        self._r, self._w = os.pipe()
        self.open_error: Optional[Exception] = None

    def open(self, path):
        if self.open_error:
            raise self.open_error
        self.calls.append(("open", path))

    def fileno(self):
        return self._w                       # a pipe's write end is always "writable": POLLOUT

    def claim(self, ifnum):
        self.calls.append(("claim", ifnum))
        return ""

    def submit(self, urb_id, endpoint, length, data=None):
        self.calls.append(("submit", urb_id, endpoint, length, data))

    def reap(self):
        return self.done.pop(0) if self.done else None

    def discard(self, urb_id):
        self.calls.append(("discard", urb_id))

    def release(self, ifnum):
        self.calls.append(("release", ifnum))

    def connect(self, ifnum):
        self.calls.append(("connect", ifnum))

    def close(self):
        self.calls.append(("close",))
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


def make_usb_tree(root: Path, *, cls="ff", sub="5d", proto="01", vid="045e", pid="028e", bus=1, dev=7,
                  ifnum=0, with_in=True) -> None:
    ddir = root / "1-2"
    ddir.mkdir(parents=True)
    for k, v in (("idVendor", vid), ("idProduct", pid), ("busnum", str(bus)), ("devnum", str(dev)),
                 ("manufacturer", "Test"), ("product", "Pad")):
        (ddir / k).write_text(v + "\n")
    idir = root / f"1-2:1.{ifnum}"
    idir.mkdir()
    for k, v in (("bInterfaceClass", cls), ("bInterfaceSubClass", sub), ("bInterfaceProtocol", proto),
                 ("bInterfaceNumber", f"{ifnum:02x}")):
        (idir / k).write_text(v + "\n")
    eps = [("ep_81", "81")] if with_in else []
    eps.append(("ep_01", "01"))
    for name, addr in eps:
        (idir / name).mkdir()
        (idir / name / "bEndpointAddress").write_text(addr + "\n")
        (idir / name / "bmAttributes").write_text("03\n")


class TestDecode(unittest.TestCase):
    def test_buttons_triggers_sticks(self):
        keys, abs_, hat = decode_report(x360_report(b2=0x10 | 0x01, b3=0x10 | 0x80, lt=10, rt=250, lx=1000, ly=2000,
                                                    rx=-3, ry=-4))
        self.assertEqual(keys, {BTN_START, BTN_SOUTH, 0x134})            # START, A, Y
        self.assertEqual(abs_[0x00], 1000)
        self.assertEqual(abs_[0x01], ~2000)                              # up is minus, as in xpad
        self.assertEqual(abs_[0x02], 10)
        self.assertEqual(abs_[0x05], 250)
        self.assertEqual(abs_[0x03], -3)
        self.assertEqual(abs_[0x04], ~-4)
        self.assertEqual(hat, (0, -1))                                   # D-pad up

    def test_dpad_directions(self):
        self.assertEqual(decode_report(x360_report(b2=0b1000))[2], (1, 0))
        self.assertEqual(decode_report(x360_report(b2=0b0110))[2], (-1, 1))      # down + left
        self.assertEqual(decode_report(x360_report())[2], (0, 0))

    def test_full_range_does_not_overflow(self):
        _, abs_, _ = decode_report(x360_report(ly=-32768, ry=32767))
        self.assertEqual(abs_[0x01], 32767)
        self.assertEqual(abs_[0x04], -32768)

    def test_other_messages_ignored(self):
        self.assertIsNone(decode_report(bytes((0x01, 0x03, 0x06)) + bytes(17)))      # LED/ack message
        self.assertIsNone(decode_report(x360_report()[:10]))
        self.assertIsNone(decode_report(bytes((0x08, 0x00)) + bytes(18)))            # connect/disconnect notice

    def test_rumble_report(self):
        self.assertEqual(rumble_report(0xFFFF, 0x8000), bytes((0, 8, 0, 0xFF, 0x80, 0, 0, 0)))
        self.assertEqual(rumble_report(0, 0), bytes((0, 8, 0, 0, 0, 0, 0, 0)))


class TestKernelAbi(unittest.TestCase):
    def test_ioctl_numbers_match_linux_usbdevice_fs_h(self):
        import ctypes
        import gamepad_remapd.input.xusb_linux as x
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("constants below are for LP64")
        self.assertEqual(ctypes.sizeof(x.Urb), 56)
        self.assertEqual(x.USBDEVFS_SUBMITURB, 0x8038550A)
        self.assertEqual(x.USBDEVFS_DISCARDURB, 0x550B)
        self.assertEqual(x.USBDEVFS_REAPURBNDELAY, 0x4008550D)
        self.assertEqual(x.USBDEVFS_CLAIMINTERFACE, 0x8004550F)
        self.assertEqual(x.USBDEVFS_RELEASEINTERFACE, 0x80045510)
        self.assertEqual(x.USBDEVFS_IOCTL, 0xC0105512)
        self.assertEqual(x.USBDEVFS_CONNECT, 0x5517)
        self.assertEqual(x.USBDEVFS_DISCONNECT_CLAIM, 0x8108551B)


class TestEnumerate(unittest.TestCase):
    def test_lists_x360_interface(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_usb_tree(root)
            infos = enumerate_xusb(str(root), "/dev/bus/usb", access=lambda p, m: True)
        self.assertEqual(len(infos), 1)
        i = infos[0]
        self.assertEqual((i.vid, i.pid, i.backend, i.interface, i.bus), (0x045E, 0x028E, "xusb", 0, 3))
        self.assertEqual(i.path, "/dev/bus/usb/001/007")
        self.assertEqual(i.name, "Test Pad")
        u = i.extra["usb"]
        self.assertEqual((u["ifnum"], u["ep_in"], u["ep_out"]), (0, 0x81, 0x01))
        self.assertTrue(evdev_format(i))

    def test_other_protocols_and_missing_endpoint_skipped(self):
        for kw in ({"sub": "47", "proto": "d0"}, {"proto": "81"}, {"cls": "03"}, {"with_in": False}):
            with tempfile.TemporaryDirectory() as d:
                make_usb_tree(Path(d), **kw)
                self.assertEqual(enumerate_xusb(d, "/dev/bus/usb", access=lambda p, m: True), [], kw)

    def test_no_access_falls_back_to_evdev(self):
        with tempfile.TemporaryDirectory() as d:
            make_usb_tree(Path(d))
            self.assertEqual(enumerate_xusb(d, "/dev/bus/usb", access=lambda p, m: False), [])

    def test_evdev_node_of_the_same_interface(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_usb_tree(root)
            xu = enumerate_xusb(str(root), "/dev/bus/usb", access=lambda p, m: True)
            iface = xu[0].extra["usb"]["sysfs"]
            mine = DeviceInfo("/dev/input/event8", 0x045E, 0x028E, backend="evdev", extra={"sysfs": iface + "/input/input23"})
            other = DeviceInfo("/dev/input/event9", 0x045E, 0x028E, backend="evdev", extra={"sysfs": "/sys/devices/other/input/input5"})
            nosys = DeviceInfo("/dev/input/event10", 0x045E, 0x028E, backend="evdev", extra={})
            self.assertTrue(evdev_node_of(mine, xu))
            self.assertFalse(evdev_node_of(other, xu))
            self.assertFalse(evdev_node_of(nosys, xu))

    def test_default_enumeration_does_not_touch_usb(self):
        import gamepad_remapd.input.xusb_linux as xl
        called = []
        orig = xl.enumerate_xusb
        xl.enumerate_xusb = lambda *a, **k: called.append(1) or []
        try:
            enumerate_devices("evdev")                       # xinput defaults to "evdev"
            self.assertEqual(called, [])
            enumerate_devices("evdev", "usb")
            self.assertEqual(called, [1])
        finally:
            xl.enumerate_xusb = orig


def info(ep_out: Optional[int] = 0x01) -> DeviceInfo:
    from gamepad_remapd.input.xusb_linux import XPAD_ABS, XPAD_KEYS
    return DeviceInfo("/dev/bus/usb/001/007", 0x045E, 0x028E, "Test Pad", interface=0, bus=3, backend="xusb",
                      extra={"keys": list(XPAD_KEYS), "abs": dict(XPAD_ABS), "range_known": True,
                             "usb": {"ifnum": 0, "ep_in": 0x81, "ep_out": ep_out, "sysfs": "/sys/x"}})


class TestDevice(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFs()
        self.dev = XusbDevice(info(), 64, fs_factory=lambda: self.fs)
        self.dev.open()
        self.addCleanup(self.dev.close)
        self.pr = ReportParser(try_layout(info()))

    def test_open_claims_and_submits_in_transfer(self):
        self.assertEqual(self.fs.calls[:3], [("open", "/dev/bus/usb/001/007"), ("claim", 0),
                                              ("submit", 1, 0x81, IN_BUF, None)])
        self.assertTrue(self.dev.rumble_ok)

    def test_report_becomes_synthetic_report_and_in_transfer_is_resubmitted(self):
        first = self.dev.read()                              # initial (neutral) state
        word, _ = self.pr.parse(first)
        self.assertEqual(word, 0)
        self.fs.done.append((1, 0, x360_report(b3=0x10, b2=0x08, lt=77, lx=12345, ly=-100)))
        raw = self.dev.read()
        word, vals = self.pr.parse(raw)
        pressed = {n for i, n in enumerate(self.pr.src_names) if n and word >> i & 1}
        self.assertEqual(pressed, {"SOUTH", "DPAD_RIGHT"})
        self.assertEqual(vals[self.pr.index["L2"]], 77)
        self.assertEqual(vals[self.pr.index["LX"]], 12345)
        self.assertEqual(vals[self.pr.index["LY"]], ~-100)
        self.assertEqual(self.fs.calls[-1], ("submit", 1, 0x81, IN_BUF, None))

    def test_batch_of_completions_queues_reports(self):
        self.dev.read()
        self.fs.done += [(1, 0, x360_report(b3=0x20)), (1, 0, x360_report())]
        a = self.pr.parse(self.dev.read())[0]
        b = self.pr.parse(self.dev.read())[0]
        self.assertNotEqual(a, 0)
        self.assertEqual(b, 0)

    def test_unknown_message_is_ignored(self):
        self.dev.read()
        self.fs.done.append((1, 0, bytes((0x01, 0x03, 0x06)) + bytes(17)))
        self.assertIsNone(self.dev.read())
        self.assertEqual(self.fs.calls[-1][:2], ("submit", 1))       # still listening

    def test_disconnect_raises_device_gone(self):
        self.dev.read()
        self.fs.done.append((1, -errno.ENODEV, b""))
        with self.assertRaises(DeviceGone):
            self.dev.read()

    def test_transient_error_is_retried_but_not_forever(self):
        self.dev.read()
        self.fs.done.append((1, -errno.EPIPE, b""))
        self.assertIsNone(self.dev.read())
        for _ in range(25):
            self.fs.done.append((1, -errno.EPIPE, b""))
            try:
                self.dev.read()
            except DeviceGone:
                return
        self.fail("persistent errors must end in DeviceGone")

    def test_wake_interrupts_read(self):
        self.dev.read()
        self.dev.wake()
        self.assertIsNone(self.dev.read())

    def test_rumble_report_is_sent_to_out_endpoint(self):
        self.dev.set_rumble(0xFF00, 0x4000)
        self.assertEqual(self.fs.calls[-1], ("submit", 2, 0x01, 8, bytes((0, 8, 0, 0xFF, 0x40, 0, 0, 0))))

    def test_rumble_update_during_transfer_is_not_lost(self):
        self.dev.read()
        self.dev.set_rumble(65535, 65535)
        self.dev.set_rumble(0, 0)                            # arrives while the first is in flight
        outs = [c for c in self.fs.calls if c[0] == "submit" and c[1] == 2]
        self.assertEqual(len(outs), 1)
        self.fs.done.append((2, 0, b""))                     # the first completes -> the stop goes out
        self.dev.read()
        outs = [c for c in self.fs.calls if c[0] == "submit" and c[1] == 2]
        self.assertEqual(outs[-1][4], rumble_report(0, 0))

    def test_close_stops_motors_releases_and_returns_driver(self):
        self.dev.set_rumble(65535, 65535)
        self.dev.close()
        names = [c[0] for c in self.fs.calls]
        self.assertLess(names.index("release"), names.index("connect"))
        self.assertEqual(names[-1], "close")
        self.assertIn(("discard", 1), self.fs.calls)
        self.assertTrue(any(c[0] == "submit" and c[4] == rumble_report(0, 0) for c in self.fs.calls))      # motors off
        self.dev.close()                                     # idempotent

    def test_no_out_endpoint_means_no_rumble(self):
        fs = FakeFs()
        d = XusbDevice(info(ep_out=None), 64, fs_factory=lambda: fs)
        d.open()
        self.addCleanup(d.close)
        self.assertFalse(d.rumble_ok)
        with self.assertRaises(OSError):
            d.set_rumble(1, 1)

    def test_permission_error_has_a_hint(self):
        fs = FakeFs()
        fs.open_error = PermissionError(errno.EACCES, "denied")
        d = XusbDevice(info(), 64, fs_factory=lambda: fs)
        with self.assertRaises(PermissionError) as cm:
            d.open()
        self.assertIn("udev", str(cm.exception))

    def test_failed_claim_closes_the_node(self):
        fs = FakeFs()
        fs.claim = lambda ifnum: (_ for _ in ()).throw(OSError(errno.EBUSY, "busy"))
        d = XusbDevice(info(), 64, fs_factory=lambda: fs)
        with self.assertRaises(OSError):
            d.open()
        self.assertEqual(fs.calls[-1], ("close",))
        self.assertNotIn("connect", [c[0] for c in fs.calls])        # never claimed: nothing to give back


class TestIntegration(unittest.TestCase):
    def test_device_id_and_protocol(self):
        i = info()
        self.assertEqual(device_id(i), "usb-001-007-0")
        lay = try_layout(i)
        self.assertIsNotNone(lay)
        d = render_protocol(describe_protocol(i, lay), "en")
        text = repr(d)
        self.assertIn("USB XInput", text)
        self.assertIn("direct USB report", text)


if __name__ == "__main__":
    unittest.main()
