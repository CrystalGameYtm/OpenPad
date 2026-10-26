"""Вібрація: шаблони з CRC32, успадкування відкаліброваними розкладками, FF_RUMBLE для evdev."""
import json
import os
import shutil
import socket
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.input.evdev_linux import EV_FF, EVIOCRMFF, EVIOCSFF, FF_RUMBLE, EvdevDevice, FFEffect
from gamepad_remapd.logic.layout import DeviceLayout, LayoutError, load_layouts

ROOT = Path(__file__).resolve().parent
LAYOUTS = ROOT.parent / "src" / "gamepad_remapd" / "layouts"
DATA = ROOT / "data"


class RumbleTemplates(unittest.TestCase):
    def test_ds4_bluetooth_report_has_valid_crc(self):
        lay = DeviceLayout.from_file(LAYOUTS / "sony_ds4_bt.json")
        r = lay.rumble.build(0xFFFF, 0x4000)
        self.assertEqual(len(r), 78)
        self.assertEqual(r[:4], bytes([0x11, 0xC4, 0x00, 0x01]))                 # id, HID+CRC+інтервал (як SDL), аудіо, лише мотори
        self.assertEqual((r[6], r[7]), (0x40, 0xFF))                              # weak, strong
        self.assertEqual(int.from_bytes(r[74:], "little"), zlib.crc32(b"\xa2" + r[:74]) & 0xFFFFFFFF)
        r2 = lay.rumble.build(0, 0)
        self.assertNotEqual(r[74:], r2[74:])                                      # CRC перераховується на кожен звіт

    def test_ds4_bluetooth_matches_sdl_capture(self):
        """Байти, які SDL3 реально пише в GPS13B (strace): сильний мотор, потім зупинка. Лайтбар SDL не
        торкаємось (у нас прапорець 0x01), тому порівнюємо мотори, службові байти й CRC після нашого лайтбара."""
        lay = DeviceLayout.from_file(LAYOUTS / "sony_ds4_bt.json")
        r = lay.rumble.build(0xFFFF, 0)
        self.assertEqual(r[:8].hex(" "), "11 c4 00 01 00 00 00 ff")
        r0 = lay.rumble.build(0, 0)
        self.assertEqual(r0[:8].hex(" "), "11 c4 00 01 00 00 00 00")

    def test_ds4_usb_touches_only_motors(self):
        lay = DeviceLayout.from_file(LAYOUTS / "sony_ds4_usb.json")
        r = lay.rumble.build(0xFFFF, 0)
        self.assertEqual(len(r), 32)
        self.assertEqual(r[1], 0x01, "прапорець 0x01 = лише мотори; 0x07 гасив би лайтбар")

    def test_bad_crc_section_rejected(self):
        d = json.loads((LAYOUTS / "sony_ds4_bt.json").read_text(encoding="utf-8"))
        d["rumble"]["crc32"] = {"seed": "zz"}
        with self.assertRaises(LayoutError):
            DeviceLayout.from_dict(d, "x")


class CalibratedInheritance(unittest.TestCase):
    def load(self, *fixtures):
        with tempfile.TemporaryDirectory() as d:
            for f in fixtures:
                shutil.copy(DATA / f, d)
            return load_layouts(LAYOUTS, Path(d))

    def test_gps13b_bluetooth_gets_rumble_and_gyro(self):
        lays = self.load("calibrated_054c_09cc_bt.json")
        mine = lays["calibrated_054c_09cc_bt"]
        self.assertIsNotNone(mine.rumble)
        self.assertEqual(mine.rumble.crc32_at, 74)
        self.assertIn("GYRO_X", mine.fields)
        self.assertEqual(mine.gyro["yaw"], "GYRO_Y")
        self.assertEqual(mine.fields["LX"].offset, 3)                  # вивчене не перезаписано
        self.assertEqual(mine.buttons["BACK"].bit, 5)                  # калібрування (BACK/START поміняно) збережене
        self.assertGreaterEqual(mine.min_len, mine.fields["ACC_Z"].end_byte)

    def test_flydigi_gets_skipped_extras_but_keeps_learned(self):
        lays = self.load("calibrated_04b4_2412_if2.json")
        mine = lays["calibrated_04b4_2412_if2"]
        self.assertIsNone(mine.rumble, "DInput Flydigi: OUT-ендпоінт не відповідає, вібрації немає")
        for n in ("M3", "M4"):
            self.assertIn(n, mine.buttons, n)
        self.assertEqual((mine.buttons["M1"].byte, mine.buttons["M1"].bit), (7, 2))

    def test_no_bit_clashes_after_inheritance(self):
        from gamepad_remapd.logic.layout import ReportParser
        for f in ("calibrated_054c_09cc_bt.json", "calibrated_04b4_2412_if2.json"):
            for lay in self.load(f).values():
                ReportParser(lay)                                      # кидає LayoutError, якщо два імені ділять біт

    def test_non_calibrated_layout_untouched(self):
        lays = load_layouts(LAYOUTS)
        self.assertEqual(lays["sony_ds4_usb"].inherited, [])


class FakeIoctl:
    def __init__(self):
        self.calls = []
        self.next_id = 7

    def __call__(self, fd, req, arg=0, mutate=False):
        self.calls.append((req, bytes(arg) if isinstance(arg, (bytes, bytearray)) else arg))
        if req == EVIOCSFF:
            e = FFEffect.from_buffer(arg)
            if e.id < 0:
                e.id = self.next_id                                    # ядро призначає id
        return 0


class EvdevRumble(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "event9", "device", "capabilities"))
        self.sys = self.tmp
        self.a, self.b = socket.socketpair()                           # a — «вузол» (читання/запис), b — «ядро»
        self.b.setblocking(False)
        self.info = DeviceInfo(path="/dev/input/event9", vid=0x045E, pid=0x028E, name="pad", backend="evdev",
                               extra={"keys": [0x130], "abs": {0: (-32768, 32767), 1: (-32768, 32767)}})

    def tearDown(self):
        self.a.close()
        self.b.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ff_caps(self, bits_hex):
        Path(self.sys, "event9", "device", "capabilities", "ff").write_text(bits_hex + "\n")

    def make(self, io):
        fd = os.dup(self.a.fileno())
        dev = EvdevDevice(self.info, opener=lambda p: fd, ioctl_fn=io, sys_root=self.sys)
        dev.open = lambda: None                                        # вузол-сокет не годиться для poll/resync
        dev._fd = fd
        dev.rumble_ok = dev._probe_rumble()
        return dev

    def events(self):
        data = self.b.recv(4096)
        return [struct.unpack_from("llHHi", data, o)[2:] for o in range(0, len(data), 24)]

    def test_rumble_uploads_effect_and_plays(self):
        self.ff_caps("%x" % (1 << FF_RUMBLE))
        io = FakeIoctl()
        dev = self.make(io)
        self.assertTrue(dev.rumble_ok)
        dev.set_rumble(0xFFFF, 0x1234)
        req, raw = io.calls[0]
        self.assertEqual(req, EVIOCSFF)
        e = FFEffect.from_buffer_copy(raw)
        self.assertEqual((e.type, e.u.rumble.strong_magnitude, e.u.rumble.weak_magnitude), (FF_RUMBLE, 0xFFFF, 0x1234))
        self.assertEqual(self.events(), [(EV_FF, 7, 1)])               # play ефекту з id, призначеним ядром
        dev.set_rumble(0x100, 0)                                       # оновлення того ж ефекту
        self.assertEqual(FFEffect.from_buffer_copy(io.calls[1][1]).id, 7)
        dev.set_rumble(0, 0)                                           # стоп
        self.assertEqual(self.events()[-1], (EV_FF, 7, 0))
        dev._stop_rumble(remove=True)
        self.assertEqual(io.calls[-1][0], EVIOCRMFF)

    def test_no_ff_capability_means_no_rumble(self):
        self.ff_caps("0")
        dev = self.make(FakeIoctl())
        self.assertFalse(dev.rumble_ok)
        with self.assertRaises(OSError):
            dev.set_rumble(1, 1)

    def test_session_routes_rumble_to_evdev_device(self):
        from gamepad_remapd.core.session import Session
        self.ff_caps("%x" % (1 << FF_RUMBLE))
        io = FakeIoctl()
        dev = self.make(io)
        s = Session.__new__(Session)                                   # лише _on_rumble: без запуску потоків
        s.layout, s.dev, s.id = None, dev, "event9"
        import threading
        s._wlock = threading.Lock()
        Session._on_rumble(s, 0xFFFF, 0)
        self.assertEqual(io.calls[0][0], EVIOCSFF)


if __name__ == "__main__":
    unittest.main()
