import io
import unittest

from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.logic.layout import load_layouts
from gamepad_remapd.tools import rumble_test
from pathlib import Path

LAY = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"


class Dev:
    def __init__(self):
        self.w, self.closed = [], False

    def write_output(self, d):
        self.w.append(bytes(d))

    def read(self):
        return b""

    def close(self):
        self.closed = True


class T(unittest.TestCase):
    def go(self, **kw):
        info = DeviceInfo(path="/dev/hidraw8", vid=0x04B4, pid=0x2412, name="f", interface=2, backend="hidraw")
        dev, out = Dev(), io.StringIO()
        rc = rumble_test.run("/dev/hidraw8", load_layouts(LAY), enumerate_fn=lambda b: [info],
                             open_fn=lambda i, n: dev, sleep=lambda s: None, out=out, **kw)
        return rc, dev, out.getvalue()

    def test_no_template_without_rumble_layout(self):
        rc, dev, _ = self.go()
        self.assertEqual(rc, 2)

    def test_explicit_template(self):
        rc, dev, _ = self.go(template="05 0f {strong} {weak}", pad_to=12)
        self.assertEqual(rc, 0)
        self.assertIn(bytes([5, 0x0F, 0xFF, 0]) + bytes(8), dev.w)
        self.assertTrue(dev.closed)

    def test_template_with_crc(self):
        import zlib
        rc, dev, _ = self.go(template="11 c4 00 01 00 00 {weak} {strong}", pad_to=78, crc32_at=74)
        r = dev.w[0] if dev.w[0][0] == 0x11 else next(w for w in dev.w if w[0] == 0x11)
        self.assertEqual(len(r), 78)
        self.assertEqual(r[74:], zlib.crc32(b"\xa2" + r[:74]).to_bytes(4, "little"))

    def test_sweep(self):
        rc, dev, _ = self.go(sweep="0x05:0x10-0x11")
        self.assertEqual({w[1] for w in dev.w} - {0xEC}, {0x10, 0x11})


if __name__ == "__main__":
    unittest.main()
