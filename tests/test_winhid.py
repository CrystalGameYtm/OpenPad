import ctypes
import unittest
from unittest import mock

from gamepad_remapd.input import winhid
from gamepad_remapd.input.base import DeviceGone, DeviceInfo


class FakeWin:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.written = []
        self.closed = False
        self.woke = False

    def paths(self):
        return [r"\\?\hid#vid_054c&pid_09cc&mi_00#7&abc#{4d1e55b2-f16f-11cf-88cb-001111000030}",
                r"\\?\hid#{00001124-0000-1000-8000-00805f9b34fb}_vid&0002d7d7_pid&0041#8&1#{x}"]

    def query(self, path):
        return 0x054C, 0x09CC, "Pad", winhid.Caps(1, 5, 65, 32, 0)

    def open(self, path):
        return (1, 2, True)

    def close(self, h, st=None):
        self.closed = True

    def read(self, st, size, t):
        return self.chunks.pop(0) if self.chunks else None

    def wake(self, st):
        self.woke = True

    def write(self, h, d):
        self.written.append(d)

    def set_feature(self, h, d):
        self.written.append(("f", d))

    def get_feature(self, h, rid, n):
        return bytes([rid]) * n


class WinHidTests(unittest.TestCase):
    def test_struct_sizes(self):
        self.assertEqual(ctypes.sizeof(winhid._HIDP_CAPS), 64)
        if ctypes.sizeof(ctypes.c_void_p) == 8 and ctypes.sizeof(ctypes.c_ulong) == 4:   # лише Windows (LLP64)
            self.assertEqual(ctypes.sizeof(winhid._SP_DEVICE_INTERFACE_DATA), 32)

    def test_parse(self):
        self.assertEqual(winhid.parse_interface(r"\\?\hid#vid_1&pid_2&mi_02#x"), 2)
        self.assertIsNone(winhid.parse_interface(r"\\?\hid#vid_1&pid_2#x"))

    def test_enumerate(self):
        infos = winhid.enumerate_winhid(FakeWin([]))
        self.assertEqual(len(infos), 2)
        self.assertEqual((infos[0].vid, infos[0].pid, infos[0].interface, infos[0].bus), (0x54C, 0x9CC, 0, 3))
        self.assertEqual(infos[1].bus, 5)
        self.assertEqual(infos[0].backend, "winhid")

    def test_read_strips_zero_id(self):
        info = winhid.enumerate_winhid(FakeWin([]))[0]
        w = FakeWin([b"\x00\x01\x02", b"\x05\x01", b""])
        d = winhid.WinHidDevice(info, 64, w)
        d.open()
        self.assertEqual(d.read(), b"\x01\x02")
        self.assertEqual(d.read(), b"\x05\x01")
        self.assertIsNone(d.read())
        d.wake()
        self.assertTrue(w.woke)
        d.close()
        self.assertTrue(w.closed)
        with self.assertRaises(DeviceGone):
            d.read()

    def test_write_padded(self):
        info = winhid.enumerate_winhid(FakeWin([]))[0]
        w = FakeWin([])
        d = winhid.WinHidDevice(info, 64, w)
        d.open()
        d.write_output(b"\x05\xff")
        self.assertEqual(len(w.written[0]), 32)

    def test_fallback_from_hidapi(self):
        from gamepad_remapd import input as inp
        from gamepad_remapd.input.base import BackendUnavailable
        with mock.patch("gamepad_remapd.input.hidapi_backend.enumerate_hidapi", side_effect=BackendUnavailable("x")), \
             mock.patch("gamepad_remapd.input.winhid.enumerate_winhid", return_value=["ok"]), \
             mock.patch.object(inp.os, "name", "nt"), mock.patch.object(inp, "_linux", return_value=False):
            self.assertEqual(inp.enumerate_devices("auto"), ["ok"])


if __name__ == "__main__":
    unittest.main()
