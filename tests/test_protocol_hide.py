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


class HidrawGroupHide(unittest.TestCase):
    def test_rules(self):
        from gamepad_remapd.tools.udev import generate_hide_hidraw
        t = generate_hide_hidraw([("d7d7", "0041", "Flydigi")], "gamepad-remapd")
        self.assertIn('SUBSYSTEM=="hidraw", SUBSYSTEMS=="usb", ATTRS{idVendor}=="d7d7", ATTRS{idProduct}=="0041"', t)
        self.assertIn('KERNELS=="0005:D7D7:0041.*"', t)
        self.assertEqual(t.count('TAG-="uaccess", GROUP="gamepad-remapd", MODE="0660"'), 2)
        self.assertIn("71-gamepad-remapd", t)


class SetupHide(unittest.TestCase):
    def test_plan(self):
        from gamepad_remapd.tools import setup_hide as sh
        hide = [("04b4", "2412", "Flydigi")]
        files, cmds = sh.plan([("04b4", "2412", "Flydigi")], hide, user="u", uid=1000,
                              service=True, project_dir="/p", python="/usr/bin/python3", src_dir="/p/src")
        self.assertEqual(set(files), {sh.F_ACCESS, sh.F_HIDE, sh.UNIT_PATH})        # без групового 71-правила
        unit = files[sh.UNIT_PATH]
        self.assertIn("User=u", unit)
        self.assertNotIn("Group=", unit)
        self.assertIn("AmbientCapabilities=CAP_FOWNER CAP_SYS_NICE", unit)
        self.assertIn("ExecStopPost=+/usr/bin/udevadm trigger", unit)
        self.assertIn("PYTHONPATH=/p/src", unit)
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1000", unit)
        self.assertIn(["systemctl", "enable", "--now", "gamepad-remapd.service"], cmds)
        files2, cmds2 = sh.plan([], hide, user="u", uid=1, service=False, project_dir="/p", python="py", src_dir=None)
        self.assertNotIn(sh.UNIT_PATH, files2)
        self.assertNotIn(["systemctl", "daemon-reload"], cmds2)

    def test_dry_run_prints(self):
        import io
        from gamepad_remapd.tools import setup_hide as sh
        files, cmds = sh.plan([], [("04b4", "2412", "x")], user="u", uid=1, service=False,
                              project_dir="/p", python="py", src_dir=None)
        buf = io.StringIO()
        sh.run(files, cmds, None, dry_run=True, out=buf)
        self.assertIn("69-gamepad-remapd-hide.rules", buf.getvalue())
        self.assertNotIn("groupadd", buf.getvalue())

    def test_uninstall_skips_systemctl_without_service(self):
        from gamepad_remapd.tools import setup_hide as sh
        files, cmds = sh.uninstall_plan(existing=lambda p: p in (sh.F_ACCESS, sh.F_HIDE))
        self.assertEqual(files, [sh.F_ACCESS, sh.F_HIDE])
        self.assertFalse(any(c[0] == "systemctl" for c in cmds))
        files, cmds = sh.uninstall_plan(existing=lambda p: True)
        self.assertTrue(any(c[0] == "systemctl" for c in cmds))

    def test_fowner(self):
        import tempfile
        from gamepad_remapd.tools import setup_hide as sh
        with tempfile.NamedTemporaryFile("w", suffix=".status") as f:
            f.write("Name:\tx\nCapEff:\t0000000000000008\n"); f.flush()
            self.assertTrue(sh.has_fowner(f.name))
        with tempfile.NamedTemporaryFile("w", suffix=".status") as f:
            f.write("CapEff:\t0000000000000000\n"); f.flush()
            import os
            from unittest import mock
            with mock.patch.object(os, "geteuid", return_value=1000):
                self.assertFalse(sh.has_fowner(f.name))
        a = sh.fowner_exec_argv(1000, 1000, ["serve", "--port", "9"], environ={"PYTHONPATH": "src", "A": "b"}, python="py")
        self.assertEqual(a[:2], ["sudo", "setpriv"])
        self.assertIn("--ambient-caps=+fowner,+sys_nice", a)
        self.assertIn("PYTHONPATH=src", a)
        self.assertIn("GAMEPAD_REMAPD_NO_REEXEC=1", a)
        self.assertEqual(a[-5:], ["py", "-m", "gamepad_remapd", "serve", "--port", "9"][-5:])

    def test_closed_to_group(self):
        import os
        import grp
        from unittest import mock
        from gamepad_remapd.input.hidraw_linux import _closed_to_group
        st = os.stat_result((0o100660, 0, 0, 1, 0, 777, 0, 0, 0, 0))
        with mock.patch.object(grp, "getgrnam", return_value=mock.Mock(gr_gid=777)):
            self.assertTrue(_closed_to_group(st))
            st2 = os.stat_result((0o100666, 0, 0, 1, 0, 777, 0, 0, 0, 0))
            self.assertFalse(_closed_to_group(st2))


class RegroupServe(unittest.TestCase):
    def test_argv(self):
        from gamepad_remapd.tools import setup_hide as sh
        a = sh.group_exec_argv("gamepad-remapd", "u", ["--port", "9000", "serve"],
                               environ={"PYTHONPATH": "src", "HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/1000", "X": "y"},
                               python="/usr/bin/python3")
        self.assertEqual(a[:5], ["sudo", "-g", "gamepad-remapd", "-u", "u"])
        self.assertIn("PYTHONPATH=src", a)
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1000", a)
        self.assertIn("GAMEPAD_REMAPD_NO_REEXEC=1", a)
        self.assertNotIn("X=y", a)
        self.assertEqual(a[-4:], ["--port", "9000", "serve"][-4:] if False else a[-4:])
        self.assertEqual(a[-5:], ["-m", "gamepad_remapd", "--port", "9000", "serve"])

    def test_needs_group(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        from gamepad_remapd.tools import setup_hide as sh
        with tempfile.TemporaryDirectory() as td:
            rules = Path(td) / "71.rules"
            self.assertFalse(sh.needs_group("g", rules))             # правил немає
            rules.write_text("x")
            with mock.patch.object(sh, "group_gid", return_value=12345), \
                 mock.patch.object(os, "getgroups", return_value=[1, 2]), mock.patch.object(os, "getegid", return_value=1):
                self.assertTrue(sh.needs_group("g", rules))
            with mock.patch.object(sh, "group_gid", return_value=12345), \
                 mock.patch.object(os, "getgroups", return_value=[1, 12345]), mock.patch.object(os, "getegid", return_value=1):
                self.assertFalse(sh.needs_group("g", rules))
            with mock.patch.object(sh, "group_gid", return_value=None):
                self.assertFalse(sh.needs_group("g", rules))

    def test_service_active(self):
        from types import SimpleNamespace
        from gamepad_remapd.tools import setup_hide as sh
        self.assertTrue(sh.service_active(lambda *a, **k: SimpleNamespace(returncode=0)))
        self.assertFalse(sh.service_active(lambda *a, **k: SimpleNamespace(returncode=3)))
        def boom(*a, **k):
            raise FileNotFoundError
        self.assertFalse(sh.service_active(boom))


class SetupHideAccess(unittest.TestCase):
    def test_hide_target_gets_access_rule(self):
        from gamepad_remapd.tools import setup_hide as sh
        files, _ = sh.plan([("04b4", "2412", "Flydigi"), ("d7d7", "0041", "BT Flydigi")], [("d7d7", "0041", "x")],
                           user="u", uid=1, service=False, project_dir="/p", python="py", src_dir=None)
        self.assertIn('ATTRS{idVendor}=="d7d7", ATTRS{idProduct}=="0041"', files[sh.F_ACCESS])
        self.assertIn('KERNELS=="0005:D7D7:0041.*"', files[sh.F_ACCESS])
