"""DualShock 4 по Bluetooth (звіт 0x11): розкладка, вибір за шиною, наскрізь через демона.
Зразки звітів — з реального логу користувача (handshake-probe на hidraw8)."""
from __future__ import annotations

import os
import tempfile
import unittest

from gamepad_remapd import constants as C
from gamepad_remapd.config import BUILTIN_LAYOUTS
from gamepad_remapd.core.manager import Manager
from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.logic.layout import DeviceLayout, LayoutError, ReportParser, load_layouts
from gamepad_remapd.tools.monitor_cli import _find_layout

from .fakes import wait_frames

REAL = [bytes.fromhex(h.replace(" ", "")) for h in (
    "11 c0 00 80 7f 80 7f 48 00 24 00 00 e9 73 00 05 01 c1 ff 64 fd 5d 0a e0 1a 3a f2 00 00 00 00 00 01 00 00 01 a0 87 3c 74 26 87 74 17 00 00 80 00 00 00 80 00 00 00 00 80 00 00 00 80 00 00 00 00",
    "11 c0 00 80 7f 80 7f 48 00 28 00 00 a2 76 00 e0 00 cd ff 82 fd 3d 0a 00 1b 6e f2 00 00 00 00 00 01 00 00 01 a0 87 3c 74 26 87 74 17 00 00 80 00 00 00 80 00 00 00 00 80 00 00 00 80 00 00 00 00",
    "11 c0 00 80 7f 80 7f 48 00 30 00 00 14 7c 00 90 00 f8 ff 02 fe b8 09 df 1a df f2 00 00 00 00 00 01 00 00 01 a0 87 3c 74 26 87 74 17 00 00 80 00 00 00 80 00 00 00 00 80 00 00 00 80 00 00 00 00",
)]
BT = DeviceInfo(path="/dev/hidraw8", vid=0x054C, pid=0x05C4, name="Wireless Controller", interface=None, bus=5, backend="hidraw")
USB = DeviceInfo(path="/dev/hidraw9", vid=0x054C, pid=0x05C4, name="Wireless Controller", interface=None, bus=3, backend="hidraw")


class Ds4BtLayoutTests(unittest.TestCase):
    def setUp(self):
        self.bt = DeviceLayout.from_file(BUILTIN_LAYOUTS / "sony_ds4_bt.json")
        self.usb = DeviceLayout.from_file(BUILTIN_LAYOUTS / "sony_ds4_usb.json")

    def test_old_usb_layout_silently_dropped_every_bt_report(self):
        """Корінь вади: USB-розкладка чекає Report ID 1, а BT шле 0x11."""
        pu = ReportParser(self.usb)
        self.assertTrue(all(r[0] != pu.rid for r in REAL))

    def test_real_reports_parse_as_circle_with_centered_sticks(self):
        pr = ReportParser(self.bt)
        for raw in REAL:
            self.assertTrue(len(raw) >= pr.min_len and raw[0] == pr.rid)
            word, vals = pr.parse(raw)
            pressed = [n for i, n in enumerate(pr.src_names) if n and word >> i & 1]
            self.assertEqual(pressed, ["CIRCLE"])                    # 0x48: колечко + hat «нейтраль»
            v = {n: vals[i] for n, i in pr.index.items()}
            self.assertEqual((v["LX"], v["LY"], v["RX"], v["RY"], v["L2"], v["R2"]), (128, 127, 128, 127, 0, 0))
        self.assertEqual(self.bt.buttons["CIRCLE"].target, "B")

    def test_layout_choice_follows_bus(self):
        layouts = load_layouts(BUILTIN_LAYOUTS)
        self.assertEqual(_find_layout(BT, layouts).id, "sony_ds4_bt")
        self.assertEqual(_find_layout(USB, layouts).id, "sony_ds4_usb")
        unknown = DeviceInfo(path="/x", vid=0x054C, pid=0x05C4, bus=None, backend="hidapi")
        self.assertIn(_find_layout(unknown, layouts).id, ("sony_ds4_bt", "sony_ds4_usb"))   # шина невідома — не відмовляємо

    def test_bus_matching_rules(self):
        self.assertFalse(self.usb.matches(0x054C, 0x05C4, None, 5))
        self.assertTrue(self.usb.matches(0x054C, 0x05C4, None, 3))
        self.assertTrue(self.bt.matches(0x054C, 0x09CC, None, 5))
        self.assertTrue(self.bt.matches(0x054C, 0x05C4, None))      # старий виклик без шини працює

    def test_bad_bus_is_rejected(self):
        with self.assertRaises(LayoutError):
            DeviceLayout.from_dict({"match": {"vid": "1", "pid": "2", "bus": "wifi"}}, "x")


class Ds4BtEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_bt_ds4_reaches_virtual_pad_through_the_daemon(self):
        from .test_integration import Rig, kv
        with tempfile.TemporaryDirectory() as td:
            rig = Rig(td)
            await rig.start()
            try:
                rig.present.append(BT)
                await rig.wait_for(lambda: "hidraw8" in rig.mgr.sessions)
                self.assertEqual(rig.mgr.sessions["hidraw8"].layout.id, "sony_ds4_bt")
                dev = rig.devices["/dev/hidraw8"]
                idle = bytearray(REAL[0]); idle[7] = 0x08              # нічого не натиснуто, hat нейтраль
                cross = bytearray(REAL[0]); cross[7] = 0x28            # хрестик
                dev.inject(bytes(idle))
                dev.inject(bytes(cross))
                ev = kv(wait_frames(rig.r, 1)[0])
                self.assertEqual(ev[(C.EV_KEY, C.BTN_SOUTH)], 1)
            finally:
                await rig.stop()


if __name__ == "__main__":
    unittest.main()
