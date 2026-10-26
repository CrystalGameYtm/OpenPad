"""Інтеграційні тести: Manager + Session + ControlServer + ControlClient без заліза.

Замість /dev/hidrawN — FakeDevice (черга), замість /dev/uinput — pipe (UinputPad(fd=...)).
Усе інше — справжній код: потік-читач, Pipeline, hot-swap профілів, IPC через UNIX-сокет.
"""
import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from gamepad_remapd import constants as C
from gamepad_remapd.config import (BUILTIN_LAYOUTS, BUILTIN_PROFILES, ConfigError, DaemonConfig,
                                   load_config, read_or_create_token)
from gamepad_remapd.core.manager import Manager
from gamepad_remapd.daemon import load_layouts_safe
from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.ipc.client import ControlClient, ControlError
from gamepad_remapd.ipc.server import ControlServer
from gamepad_remapd.output.uinput_linux import SPECS, UinputPad
from gamepad_remapd.store import ProfileStore, StoreError
from gamepad_remapd.web.security import origin_allowed

from .fakes import FakeDevice, drain, ds4_info, ds4_report, wait_frames


def kv(frame):
    return {(t, c): v for t, c, v in frame}


class Rig:
    """Збирає повний демон (без залізних backend-ів) у каталозі tmp."""

    def __init__(self, tmp: str, endpoint: str = None, token: str = None):
        self.tmp = Path(tmp)
        self.cfg = DaemonConfig(config_dir=self.tmp, control_endpoint=endpoint or f"unix:{tmp}/c.sock",
                                poll_interval_s=0.05, realtime=False, grab_siblings=False)
        self.token = token
        self.present = []                 # що «підключено» (повертає enumerate)
        self.devices = {}                 # path -> FakeDevice
        self.r = None                     # читальний кінець pipe «віртуального uinput»
        self.fail_open = None             # виняток, який кидає open_fn

    def _enumerate(self, backend):
        return list(self.present)

    def _open(self, info, read_size):
        if self.fail_open:
            raise self.fail_open
        d = FakeDevice(info, read_size)
        self.devices[info.path] = d
        return d

    def _output(self, kind, name=None, uinput_path=None):
        r, w = os.pipe()
        os.set_blocking(r, False)
        self.r = r
        return UinputPad(SPECS[kind], fd=w, name=name)

    async def start(self):
        layouts = load_layouts_safe([BUILTIN_LAYOUTS])
        self.store = ProfileStore(self.cfg.profiles_dir, BUILTIN_PROFILES, self.cfg.state_path)
        self.mgr = Manager(self.cfg, self.store, layouts, enumerate_fn=self._enumerate,
                           open_fn=self._open, output_fn=self._output)
        self.srv = ControlServer(self.mgr, self.store, self.cfg.endpoint(), self.token)
        await self.srv.start()
        await self.mgr.start()
        self.events = []
        self.client = ControlClient(self.cfg.endpoint(), self.token, on_event=self.events.append,
                                    subscribe=["state", "devices", "profile", "stats"])
        self.client.start()
        await asyncio.wait_for(self.client.connected.wait(), 3)

    async def stop(self):
        await self.client.close()
        await self.mgr.stop()
        await self.srv.stop()

    async def wait_for(self, pred, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("умова не виконалась за %.1f с" % timeout)

    async def plug(self):
        self.present.append(ds4_info())
        await self.wait_for(lambda: "hidraw7" in self.mgr.sessions)
        return self.devices["/dev/hidraw7"]


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.rig = Rig(self._td.name)
        await self.rig.start()

    async def asyncTearDown(self):
        await self.rig.stop()
        self._td.cleanup()

    async def test_hotplug_opens_device_with_layout_and_default_profile(self):
        await self.rig.plug()
        devs = await self.rig.client.call("devices")
        self.assertEqual(len(devs), 1)
        self.assertTrue(devs[0]["open"])
        self.assertEqual(devs[0]["layout"], "sony_ds4_usb")
        self.assertEqual(devs[0]["profile"], "default")
        self.assertIn("CROSS", [n for _, n in devs[0]["inputs"]])

    async def test_report_goes_through_to_virtual_pad(self):
        dev = await self.rig.plug()
        dev.inject(ds4_report(cross=True, hat=2))
        fr = wait_frames(self.rig.r, 1)[0]
        ev = kv(fr)
        self.assertEqual(ev[(C.EV_KEY, C.BTN_SOUTH)], 1)
        self.assertEqual(ev[(C.EV_ABS, C.ABS_HAT0X)], 1)           # D-pad вправо -> hat

    async def test_json_patch_hot_swaps_mapping_without_restart(self):
        dev = await self.rig.plug()
        dev.inject(ds4_report(cross=True))
        wait_frames(self.rig.r, 1)
        res = await self.rig.client.call("patch_profile", device="hidraw7",
                                         patch={"button_map": {"CROSS": "B"}})
        self.assertEqual(res["data"]["button_map"]["CROSS"], ["B"])
        dev.inject(ds4_report(cross=True))               # кнопка ВСЕ ЩЕ утримується
        evs = [e for f in wait_frames(self.rig.r, 2) for e in f]
        keys = [(c, v) for t, c, v in evs if t == C.EV_KEY]
        self.assertIn((C.BTN_SOUTH, 0), keys, "стара ціль відпущена при підміні профілю")
        self.assertIn((C.BTN_EAST, 1), keys, "нова ціль натиснута без відпускання кнопки")

    async def test_deadzone_change_applies_live(self):
        dev = await self.rig.plug()
        await self.rig.client.call("patch_profile", device="hidraw7",
                                   patch={"sticks": {"left": {"inner_deadzone": 0.5}}})
        dev.inject(ds4_report(lx=128 + 40))                       # ~31% < 50% -> тиша
        time.sleep(0.15)
        self.assertEqual(drain(self.rig.r), [])
        await self.rig.client.call("patch_profile", device="hidraw7",
                                   patch={"sticks": {"left": {"inner_deadzone": 0.05}}})
        dev.inject(ds4_report(lx=128 + 41))
        ev = kv(wait_frames(self.rig.r, 1)[0])
        self.assertGreater(ev[(C.EV_ABS, C.ABS_X)], 0)

    async def test_invalid_patch_is_rejected_and_profile_unchanged(self):
        await self.rig.plug()
        with self.assertRaises(ControlError) as cm:
            await self.rig.client.call("patch_profile", device="hidraw7",
                                       patch={"sticks": {"left": {"inner_deadzone": 5}}})
        self.assertEqual(cm.exception.code, "profile")
        r = await self.rig.client.call("active_profile", device="hidraw7")
        self.assertLess(r["data"]["sticks"]["left"]["inner_deadzone"], 1)

    async def test_state_events_reach_subscriber(self):
        dev = await self.rig.plug()
        dev.inject(ds4_report(cross=True))
        await self.rig.wait_for(lambda: any(e["event"] == "state" and "A" in e["out"]
                                            for e in self.rig.events))

    async def test_sniffer_over_ipc_finds_button(self):
        dev = await self.rig.plug()
        await self.rig.client.call("sniff", device="hidraw7", action="start", baseline_s=0.2)
        for _ in range(15):                                        # еталон спокою
            dev.inject(ds4_report())
            await asyncio.sleep(0.02)
        await self.rig.client.call("sniff", device="hidraw7", action="learn", name="M1")
        raw = bytearray(ds4_report())
        raw[30] = 0x10                                              # «прихована» кнопка: байт 30, біт 4
        for _ in range(6):
            dev.inject(bytes(raw))
            await asyncio.sleep(0.02)
        s = await self.rig.client.call("sniff", device="hidraw7", action="summary")
        self.assertEqual(s["learned"]["M1"]["byte"], 30)
        self.assertEqual(s["learned"]["M1"]["bit"], 4)

    async def test_rumble_passthrough_builds_ds4_output_report(self):
        dev = await self.rig.plug()
        out = self.rig.mgr.sessions["hidraw7"].out
        out._effects[1] = (0xFFFF, 0x8000, 0, 0)                   # ефект, «завантажений» грою
        out.handle_ff_event(C.EV_FF, 1, 1)                          # play
        self.assertEqual(len(dev.outputs), 1)
        rep = dev.outputs[0]
        self.assertEqual(len(rep), 32)
        self.assertEqual(rep[:6], bytes([0x05, 0x01, 0x00, 0x00, 0x80, 0xFF]))   # weak, strong
        out.handle_ff_event(C.EV_FF, 1, 0)                          # stop
        self.assertEqual(dev.outputs[-1][4:6], b"\x00\x00")

    async def test_unplug_releases_held_buttons_and_reaps_session(self):
        dev = await self.rig.plug()
        dev.inject(ds4_report(cross=True))
        wait_frames(self.rig.r, 1)
        self.rig.present.clear()
        dev.unplug()
        await self.rig.wait_for(lambda: "hidraw7" not in self.rig.mgr.sessions)
        ev = [e for f in wait_frames(self.rig.r, 1) for e in f]
        self.assertIn((C.EV_KEY, C.BTN_SOUTH, 0), ev, "гра не повинна лишитися із залипшою кнопкою")

    async def test_failed_open_does_not_crash_manager(self):
        """Регресія: PermissionError (немає udev-правил) раніше валив старт демона."""
        self.rig.fail_open = PermissionError(13, "Permission denied")
        self.rig.present.append(ds4_info())
        await self.rig.mgr.scan()                                  # не має кидати
        devs = await self.rig.client.call("devices")
        self.assertFalse(devs[0]["open"])
        self.assertIn("PermissionError", devs[0]["error"])

    async def test_one_bad_device_does_not_block_the_next(self):
        calls = []
        orig = self.rig._open

        def flaky(info, size):
            calls.append(info.path)
            if info.path == "/dev/hidraw6":
                raise PermissionError(13, "denied")
            return orig(info, size)
        self.rig.mgr._open_fn = flaky
        bad = DeviceInfo("/dev/hidraw6", 0x054C, 0x09CC, "bad", 0, 3, "hidraw")
        self.rig.present += [bad, ds4_info()]
        await self.rig.mgr.scan()
        self.assertIn("hidraw7", self.rig.mgr.sessions)
        self.assertNotIn("hidraw6", self.rig.mgr.sessions)

    async def test_ds4_09cc_matches_layout(self):
        info = DeviceInfo("/dev/hidraw8", 0x054C, 0x09CC, "DS4 v2", 0, 3, "hidraw")
        self.assertEqual(self.rig.mgr.pick_layout(info).id, "sony_ds4_usb")

    async def test_profile_crud_via_ipc(self):
        c = self.rig.client
        saved = await c.call("profile_save", name="mine", data={"sticks": {"left": {"inner_deadzone": 0.2}}})
        self.assertEqual(saved["name"], "mine")
        self.assertIn("mine", [p["name"] for p in await c.call("profiles")])
        self.assertEqual((await c.call("profile_get", name="mine"))["sticks"]["left"]["inner_deadzone"], 0.2)
        await c.call("profile_delete", name="mine")
        with self.assertRaises(ControlError):
            await c.call("profile_get", name="mine")

    async def test_unknown_command_and_missing_param_report_errors(self):
        with self.assertRaises(ControlError):
            await self.rig.client.call("нема_такої")
        with self.assertRaises(ControlError) as cm:
            await self.rig.client.call("profile_get")
        self.assertEqual(cm.exception.code, "param")


class TcpAuth(unittest.IsolatedAsyncioTestCase):
    """Windows-режим: TCP-канал із токеном."""

    async def test_token_required(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with tempfile.TemporaryDirectory() as td:
            rig = Rig(td, endpoint=f"tcp:127.0.0.1:{port}", token="s3cret")
            await rig.start()
            try:
                bad = ControlClient(rig.cfg.endpoint(), "wrong")
                bad.start()
                await asyncio.sleep(0.4)
                self.assertFalse(bad.connected.is_set(), "з невірним токеном з'єднання не встановлюється")
                await bad.close()
                self.assertEqual((await rig.client.call("ping"))["pong"], True)
            finally:
                await rig.stop()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        t = Path(self.td.name)
        self.st = ProfileStore(t / "u", BUILTIN_PROFILES, t / "state.json")

    def tearDown(self):
        self.td.cleanup()

    def test_save_get_list_delete(self):
        self.st.save("mine", {"description": "x"})
        self.assertEqual(self.st.get("mine")["description"], "x")
        names = {p["name"]: p["source"] for p in self.st.list()}
        self.assertEqual(names["mine"], "user")
        self.assertEqual(names["default"], "builtin")
        self.st.delete("mine")
        with self.assertRaises(StoreError):
            self.st.get("mine")
        with self.assertRaises(StoreError):
            self.st.delete("default")             # вбудовані не видаляються

    def test_user_profile_overrides_builtin(self):
        self.st.save("default", {"description": "мій"})
        self.assertEqual(self.st.get("default")["description"], "мій")

    def test_path_traversal_rejected(self):
        for bad in ("../x", "a/b", "..", "", "x" * 80, "/etc/passwd"):
            with self.assertRaises(StoreError, msg=bad):
                self.st.get(bad)
            with self.assertRaises((StoreError, Exception), msg=bad):
                self.st.save(bad, {})

    def test_active_profile_state_roundtrip(self):
        self.st.set_active("054c:09cc", "precision_aim")
        self.assertEqual(self.st.get_active("054c:09cc"), "precision_aim")
        self.assertIsNone(self.st.get_active("dead:beef"))

    def test_invalid_profile_not_written(self):
        with self.assertRaises(Exception):
            self.st.save("bad", {"sticks": {"left": {"inner_deadzone": 9}}})
        self.assertFalse((Path(self.td.name) / "u" / "bad.json").exists())


class ConfigTests(unittest.TestCase):
    def test_unknown_key_and_range_errors(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "daemon.json"
            p.write_text(json.dumps({"web_prot": 1}))
            with self.assertRaises(ConfigError):
                load_config(p)
            p.write_text(json.dumps({"web_port": 70000}))
            with self.assertRaises(ConfigError):
                load_config(p)
            p.write_text(json.dumps({"_comment": "ok", "web_port": 9000, "backend": "hidraw"}))
            self.assertEqual(load_config(p).web_port, 9000)

    def test_example_config_loads(self):
        ex = Path(__file__).resolve().parent.parent / "config" / "daemon.example.json"
        self.assertEqual(load_config(ex).web_host, "127.0.0.1")

    def test_token_created_once_with_private_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t"
            a = read_or_create_token(p)
            self.assertEqual(a, read_or_create_token(p))
            if os.name == "posix":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)


class SecurityTests(unittest.TestCase):
    A = ["127.0.0.1:8765", "localhost:8765"]

    def test_cases(self):
        ok = lambda h, o: origin_allowed(h, o, self.A)       # noqa: E731
        self.assertTrue(ok("127.0.0.1:8765", None))                       # curl / скрипти
        self.assertTrue(ok("localhost:8765", "http://localhost:8765"))   # наш UI
        self.assertFalse(ok("evil.example:8765", None), "DNS rebinding: чужий Host")
        self.assertFalse(ok("127.0.0.1:8765", "http://evil.example"), "cross-site запит")
        self.assertFalse(ok("127.0.0.1:8765", "null"))
        self.assertFalse(ok(None, None))


if __name__ == "__main__":
    unittest.main()
