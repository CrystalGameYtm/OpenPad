import json
import unittest
from pathlib import Path

from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.logic.layout import DeviceLayout
from gamepad_remapd.logic.protocol import describe_protocol
from gamepad_remapd.tools.udev import generate_hide, hide_rule

LAYOUTS = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"


class Protocol(unittest.TestCase):
    def test_ds4_bluetooth(self):
        lay = DeviceLayout.from_file(LAYOUTS / "sony_ds4_bt.json")
        info = DeviceInfo(path="/dev/hidraw6", vid=0x054C, pid=0x09CC, name="Wireless Controller", bus=5, backend="hidraw")
        d = describe_protocol(info, lay)
        self.assertIn("Bluetooth", d["summary"])
        self.assertIn("0x11", d["summary"])
        items = dict(d["items"])
        self.assertEqual(items["Підключення"], "Bluetooth")
        self.assertEqual(items["Розмір звіту"], "78 Б")
        self.assertEqual(items["Вібрація"], "hidraw-звіт з CRC-32")
        self.assertEqual(items["Гіроскоп"], "так")
        json.dumps(d)                                      # іде в JSON API

    def test_flydigi_vendor_interface(self):
        lay = DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")
        info = DeviceInfo(path="/dev/hidraw8", vid=0x04B4, pid=0x2412, name="Flydigi", interface=2, bus=3, backend="hidraw")
        items = dict(describe_protocol(info, lay)["items"])
        self.assertEqual(items["HID-інтерфейс"], "2")
        self.assertEqual(items["Handshake"], "1 кр.")
        self.assertEqual(items["Вібрація"], "немає")
        self.assertIn("Маркер звіту", items)

    def test_no_layout(self):
        info = DeviceInfo(path="/dev/hidraw1", vid=1, pid=2, name="X", bus=3, backend="hidraw")
        d = describe_protocol(info, None)
        self.assertIn("розкладки немає", d["summary"])

    def test_evdev(self):
        info = DeviceInfo(path="/dev/input/event13", vid=0x045E, pid=0x028E, name="Pad", bus=3, backend="evdev")
        d = describe_protocol(info, None)
        self.assertIn("evdev", d["summary"])


class Hide(unittest.TestCase):
    def test_rule(self):
        r = hide_rule("054C", "9cc", "GPS13B")
        self.assertIn('ATTRS{id/vendor}=="054c"', r)
        self.assertIn('ATTRS{id/product}=="09cc"', r)
        self.assertIn('DEVPATH!="/devices/virtual/*"', r)          # свій віртуальний пристрій не ховаємо
        self.assertIn('ENV{ID_INPUT_JOYSTICK}=""', r)
        self.assertIn("SDL_GAMECONTROLLER_IGNORE_DEVICES=0x054c/0x09cc", r)

    def test_dedup(self):
        out = generate_hide([("054c", "09cc", "a"), ("054C", "09CC", "b")])
        self.assertEqual(out.count('ATTRS{id/vendor}'), 1)


if __name__ == "__main__":
    unittest.main()


class BluetoothAccess(unittest.TestCase):
    def test_hidraw_rule_has_usb_and_bluetooth(self):
        from gamepad_remapd.tools.udev import hidraw_rule
        r = hidraw_rule("d7d7", "0041", "Flydigi BT")
        self.assertIn('SUBSYSTEMS=="usb", ATTRS{idVendor}=="d7d7", ATTRS{idProduct}=="0041"', r)
        self.assertIn('KERNELS=="0005:D7D7:0041.*"', r)          # Bluetooth: BUS_BLUETOOTH = 0005
        self.assertIn('KERNELS=="0005:D7D7:*"', hidraw_rule("d7d7", None))
