"""Автопошук handshake: бібліотека, рушій (автономний потік / сигнал від користувача), збереження, UI-API."""
from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gamepad_remapd.input.base import DeviceInfo
from gamepad_remapd.logic.handshake_lib import (BUILTIN_LIBRARY, Expect, HandshakeLibError, bind_layout,
                                                candidates_for, load_library)
from gamepad_remapd.logic.layout import DeviceLayout
from gamepad_remapd.tools import handshake_probe as hp
from gamepad_remapd.tools.testpage import TestPage

from .fakes import FakeDevice
from .test_autolayout import v1_report

LAYOUTS = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"
VENDOR_FFA0 = bytes.fromhex("06 a0 ff 09 01 a1 01 15 00 26 ff 00 75 08 95 20 09 01 81 02 c0")
PLAIN_PAD = bytes.fromhex("05 01 09 05 a1 01 09 30 15 00 26 ff 00 75 08 95 01 81 02 c0")


def info(vid=0x04B4, pid=0x2412, iface=2, desc=VENDOR_FFA0, path="/dev/hidraw10") -> DeviceInfo:
    return DeviceInfo(path=path, vid=vid, pid=pid, name="Dongle", interface=iface, backend="hidraw", descriptor=desc)


class FakeDongle(FakeDevice):
    """Мовчить, доки не отримає свій handshake; після нього шле звіти (одразу або лише під час «дій користувача»)."""

    def __init__(self, info_, trigger: bytes = b"", reports=None, needs_user=False, need_write=True):
        super().__init__(info_)
        self.trigger, self.make, self.needs_user = trigger, reports, needs_user
        self.armed = not trigger                  # без trigger-а пристрій шле звіти сам
        self.user_active = False
        self.can_write = need_write
        self._stop = threading.Event()
        self._n = 0
        self.opens = 0
        threading.Thread(target=self._feed, daemon=True).start()

    def write_output(self, data: bytes) -> None:
        super().write_output(data)
        if self.trigger and data.startswith(self.trigger):
            self.armed = True
            self.inject(bytes(32))                # «відповідь» на сам handshake (службова)

    def _feed(self) -> None:
        while not self._stop.is_set():
            if self.armed and (self.user_active or not self.needs_user) and self.make:
                self._n += 1
                self.inject(self.make(self._n))
            time.sleep(0.004)

    def shutdown(self) -> None:
        self._stop.set()


def v1_stream(n: int) -> bytes:
    return v1_report(lx=(n * 3) & 0xFF)


def gamesir_stream(n: int) -> bytes:
    return bytes([0xA1, 0xC8, n & 0xFF]) + bytes(20)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.lib = {c.id: c for c in load_library(BUILTIN_LIBRARY)}

    def test_builtin_library_loads_with_exact_bytes(self):
        v1 = self.lib["flydigi_v1"]
        self.assertEqual(v1.steps[0].data, bytes.fromhex("05ec") + bytes(10))
        self.assertEqual(v1.layout, "flydigi_v1_2g4")
        sw = self.lib["switch_usb"]
        self.assertEqual([s.data[:2].hex() for s in sw.steps if s.op == "output"], ["8002", "8003", "8002", "8004", "0100"])
        self.assertTrue(all(len(s.data) == 64 for s in sw.steps if s.op == "output"))
        self.assertEqual(self.lib["gamesir_mode"].steps[0].data[:3], bytes([0xA2, 0x01, 0x00]))
        self.assertEqual(self.lib["flydigi_v2"].steps[4].data[:9], bytes.fromhex("035aa51c17015344 4c".replace(" ", "")))
        self.assertTrue(self.lib["none"].is_noop)
        self.assertEqual(self.lib["8bitdo_feature_06"].steps[0].op, "feature_get")

    def test_scope_rules(self):
        lib = list(self.lib.values())
        ids = lambda i, **k: [c.id for c in candidates_for(i, lib, **k)]
        self.assertEqual(ids(info()), ["none", "flydigi_v1", "8bitdo_feature_06", "8bitdo_feature_30"])
        self.assertNotIn("flydigi_v1", ids(info(desc=PLAIN_PAD)))                   # не вендорський інтерфейс
        self.assertNotIn("flydigi_v1", ids(info(vid=0x1234)))                       # чужий VID
        self.assertNotIn("gamesir_mode", ids(info(vid=0x1234)))                     # opt-in без широкого перебору
        broad = ids(info(vid=0x1234), broad=True)
        self.assertEqual(broad[0], "none")
        self.assertTrue({"gamesir_mode", "switch_usb", "flydigi_v2"} <= set(broad))
        self.assertIn("flydigi_v1", ids(info(desc=None)))                           # без дескриптора не відсікаємо

    def test_expect_filters_handshake_replies(self):
        e = self.lib["flydigi_v1"].expect
        self.assertTrue(e.matches(v1_report()))
        self.assertFalse(e.matches(bytes([4, 0xFF]) + bytes(30)))                  # відповідь на GET_INFO
        self.assertFalse(e.matches(b"\x04\xfe"))                                    # закоротка
        self.assertTrue(self.lib["gamesir_mode"].expect.matches(bytes([0x43, 0xA1, 0xC8, 0])))
        self.assertTrue(Expect().matches(b""))

    def test_bad_library_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "x.json").write_text(json.dumps({"candidates": [{"id": "a", "scope": "evil", "steps": []}]}))
            with self.assertRaises(HandshakeLibError):
                load_library(Path(td))
            (Path(td) / "x.json").write_text(json.dumps({"candidates": [
                {"id": "a", "scope": "any", "steps": [{"op": "output", "data": "zz"}]}]}))
            with self.assertRaises(HandshakeLibError):
                load_library(Path(td))
            (Path(td) / "x.json").write_text(json.dumps({"candidates": [
                {"id": "a", "scope": "any", "steps": [{"op": "sleep_ms", "ms": 99999}]}]}))
            with self.assertRaises(HandshakeLibError):
                load_library(Path(td))

    def test_user_library_overrides_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "mine.json").write_text(json.dumps({"candidates": [
                {"id": "flydigi_v1", "scope": "any", "steps": [{"op": "output", "data": "aa bb"}]}]}))
            lib = {c.id: c for c in load_library(BUILTIN_LIBRARY, Path(td))}
        self.assertEqual(lib["flydigi_v1"].steps[0].data, b"\xaa\xbb")


class EngineTests(unittest.TestCase):
    def probe(self, dev, info_=None, **kw):
        kw.setdefault("auto_s", 0.35)
        kw.setdefault("user_s", 0.35)
        kw.setdefault("grace_s", 0)
        events = []
        layouts = {"flydigi_v1_2g4": DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")}
        p = hp.HandshakeProbe(info_ or dev.info, layouts=layouts, open_fn=lambda i, s: dev,
                              emit=lambda e: (events.append(e), e["kind"] == "prompt" and setattr(dev, "user_active", True)),
                              **kw)
        self.addCleanup(dev.shutdown)
        return p, events

    def test_flydigi_v1_found_autonomously_and_layout_recognised(self):
        dev = FakeDongle(info(), trigger=bytes.fromhex("05ec"), reports=v1_stream)
        p, events = self.probe(dev, use_user=False)               # режим без користувача: пристрій шле потік сам
        r = p.run()
        self.assertTrue(r.ok, r.message)
        self.assertEqual((r.candidate, r.mode, r.layout), ("flydigi_v1", "handshake", "flydigi_v1_2g4"))
        self.assertGreaterEqual(r.layout_match, 0.99)
        self.assertEqual(dev.outputs[0], bytes.fromhex("05ec") + bytes(10))
        self.assertEqual(r.report_len, 32)
        self.assertTrue(r.samples[0].startswith("04 fe"))
        self.assertEqual([t["id"] for t in r.tried][:2], ["none", "flydigi_v1"])       # none не оживив, v1 — так
        self.assertNotIn("prompt", [e["kind"] for e in events])                          # користувача не турбували
        self.assertEqual([e["phase"] for e in events if e["kind"] == "try"], ["auto", "auto"])
        self.assertTrue(dev.closed)

    def test_user_prompt_is_shown_once_up_front(self):
        dev = FakeDongle(info(), trigger=bytes.fromhex("05ec"), reports=v1_stream)
        p, events = self.probe(dev)
        r = p.run()
        self.assertTrue(r.ok, r.message)
        self.assertEqual([e["kind"] for e in events if e["kind"] == "prompt"], ["prompt"])
        self.assertEqual(events[1]["kind"], "prompt")                                    # одразу після start

    def test_silent_until_user_acts_is_attributed_to_the_right_candidate(self):
        dev = FakeDongle(info(vid=0x3537, pid=0x1010, desc=PLAIN_PAD), trigger=bytes([0xA2, 1, 0]),
                         reports=gamesir_stream, needs_user=True)
        p, events = self.probe(dev, broad=True)
        r = p.run()
        self.assertTrue(r.ok, r.message)
        self.assertEqual((r.candidate, r.mode), ("gamesir_mode", "handshake"))
        # усі попередні кандидати (none, read-only, Flydigi V2...) НЕ отримали заслуги: пристрій тоді ще спав
        self.assertTrue(all(not t["ok"] for t in r.tried[:-1]))
        self.assertEqual(r.tried[0]["id"], "none")
        self.assertIn("prompt", [e["kind"] for e in events])
        self.assertIsNone(r.layout)                                                       # готової розкладки немає
        self.assertIn("калібрування", r.message)

    def test_device_that_works_without_handshake(self):
        dev = FakeDongle(info(vid=0x1234, pid=1, desc=PLAIN_PAD), reports=gamesir_stream)
        r = self.probe(dev)[0].run()
        self.assertTrue(r.ok)
        self.assertEqual((r.candidate, r.mode), ("none", "no-handshake"))
        with self.assertRaises(ValueError):
            hp.save_learned(r, dev.info, {}, Path("."))

    def test_already_awake_device_is_flagged_not_silently_trusted(self):
        # геймпад уже розбуджений (попереднім пошуком/офіційною програмою): none «працює», але ми попереджаємо
        dev = FakeDongle(info(vid=0x1234, pid=1, desc=PLAIN_PAD), trigger=b"\xde\xad", reports=gamesir_stream,
                         needs_user=True)
        dev.armed = True
        p, _ = self.probe(dev)
        r = p.run()
        self.assertEqual((r.candidate, r.mode), ("none", "no-handshake"))
        self.assertIn("вимкніть і ввімкніть", r.message)

    def test_nothing_works_reports_clearly(self):
        dev = FakeDongle(info(vid=0x1234, pid=1, desc=PLAIN_PAD), trigger=b"\xde\xad", reports=gamesir_stream)
        r = self.probe(dev, use_user=False)[0].run()
        self.assertFalse(r.ok)
        self.assertFalse(r.error)
        self.assertIn("жоден кандидат", r.message)
        self.assertGreaterEqual(len(r.tried), 3)

    def test_missing_write_permission_is_reported(self):
        dev = FakeDongle(info(), trigger=bytes.fromhex("05ec"), reports=v1_stream, need_write=False)
        r = self.probe(dev)[0].run()
        self.assertFalse(r.ok)
        self.assertTrue(r.error)
        self.assertIn("udev", r.message)
        self.assertEqual(dev.outputs, [])                                                 # нічого не слали

    def test_evdev_is_refused(self):
        ev = DeviceInfo(path="/dev/input/event5", vid=1, pid=2, backend="evdev")
        r = hp.HandshakeProbe(ev, open_fn=lambda i, s: None).run()
        self.assertTrue(r.error)
        self.assertIn("evdev", r.message)

    def test_stop_interrupts_quickly(self):
        dev = FakeDongle(info(vid=0x1234, pid=1, desc=PLAIN_PAD), trigger=b"\xde\xad", reports=gamesir_stream)
        p, _ = self.probe(dev, auto_s=5, user_s=5, broad=True)
        out = {}
        th = threading.Thread(target=lambda: out.update(r=p.run()))
        t0 = time.time()
        th.start()
        time.sleep(0.3)
        p.stop()
        th.join(3)
        self.assertFalse(th.is_alive())
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(out["r"].message, "зупинено")


class SaveTests(unittest.TestCase):
    def test_family_member_with_new_pid_gets_learned_layout(self):
        new = info(pid=0x2499)                              # інша модель тієї ж родини
        dev = FakeDongle(new, trigger=bytes.fromhex("05ec"), reports=v1_stream)
        self.addCleanup(dev.shutdown)
        layouts = {"flydigi_v1_2g4": DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")}
        r = hp.HandshakeProbe(new, layouts=layouts, open_fn=lambda i, s: dev, user_s=0.35, grace_s=0).run()
        self.assertTrue(r.ok, r.message)
        with tempfile.TemporaryDirectory() as td:
            dest = hp.save_learned(r, new, layouts, Path(td))
            self.assertEqual(dest.name, "learned_04b4_2499_if2.json")
            lay = DeviceLayout.from_file(dest)
            self.assertTrue(lay.matches(0x04B4, 0x2499, 2))
            self.assertFalse(lay.matches(0x04B4, 0x2412, 2))
            self.assertEqual(lay.handshake[0].data, bytes.fromhex("05ec") + bytes(10))
            self.assertEqual((lay.report_id, lay.sync), (4, (1, 0xFE)))
            self.assertIn("A", lay.buttons)
            raw = json.loads(dest.read_text())
            self.assertEqual(raw["_learned"]["candidate"], "flydigi_v1")
            self.assertNotIn("_comment", raw)

    def test_wrong_layout_is_not_applied_when_stream_does_not_match(self):
        # handshake «спрацював», але потік не схожий на розкладку Flydigi -> розкладку не пропонуємо
        lib = [c for c in load_library(BUILTIN_LIBRARY) if c.id == "flydigi_v1"]
        lib[0] = type(lib[0])(**{**lib[0].__dict__, "expect": Expect()})            # без фільтра очікуваного потоку
        dev = FakeDongle(info(), trigger=bytes.fromhex("05ec"), reports=lambda n: bytes([9, n & 0xFF]) + bytes(20))
        self.addCleanup(dev.shutdown)
        layouts = {"flydigi_v1_2g4": DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")}
        r = hp.HandshakeProbe(dev.info, lib, layouts=layouts, open_fn=lambda i, s: dev, user_s=0.35, grace_s=0).run()
        self.assertTrue(r.ok)
        self.assertIsNone(r.layout)
        with self.assertRaises(ValueError):
            hp.save_learned(r, dev.info, layouts, Path("."), lib)


class CliAndPageTests(unittest.TestCase):
    def test_cli_runs_and_saves(self):
        dev = FakeDongle(info(), trigger=bytes.fromhex("05ec"), reports=v1_stream)
        self.addCleanup(dev.shutdown)
        layouts = {"flydigi_v1_2g4": DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            rc = hp.run_cli("/dev/hidraw10", layouts, Path(td), save=True, user_s=0.35, grace_s=0,
                            enumerate_fn=lambda b: [dev.info], open_fn=lambda i, s: dev, out=out)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("УСПІХ", out.getvalue())
            self.assertTrue((Path(td) / "learned_04b4_2412_if2.json").exists())
        rc = hp.run_cli("/dev/nope", layouts, Path("."), enumerate_fn=lambda b: [], out=out)
        self.assertEqual(rc, 1)

    def test_testpage_probe_save_then_autodetect_opens_learned_layout(self):
        new = info(pid=0x2499, desc=VENDOR_FFA0)
        dev = FakeDongle(new, trigger=bytes.fromhex("05ec"), reports=v1_stream)
        self.addCleanup(dev.shutdown)
        base = DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")
        with tempfile.TemporaryDirectory() as td:
            tp = TestPage({base.id: base}, enumerate_fn=lambda b: [new], open_fn=lambda i, s: dev,
                          save_dir=Path(td), probe_kwargs=dict(user_s=0.35, grace_s=0))
            self.addCleanup(tp.shutdown)
            self.assertIsNone(tp._scan()[1][new.path])                  # ще немає розкладки для цього PID
            st = tp.start_probe(new.path, False)
            self.assertTrue(st["running"])
            with self.assertRaises(ValueError):                         # другий пошук паралельно не стартує
                tp.start_probe(new.path, False)
            end = time.time() + 5
            while tp.probe_status()["running"] and time.time() < end:
                time.sleep(0.05)
            res = tp.probe_status()["result"]
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["layout"], "flydigi_v1_2g4")
            saved = tp.save_probe()
            self.assertEqual(saved["layout"], "learned_04b4_2499_if2")
            self.assertIn("learned_04b4_2499_if2", tp.layouts)
            self.assertTrue(tp.auto)
            tp.auto_tick()
            self.assertEqual(tp.probe.layout.id, "learned_04b4_2499_if2")   # тепер сторінка відкриває пристрій сама

    def test_testpage_auto_waits_while_probe_runs(self):
        new = info()
        dev = FakeDongle(new, trigger=b"\xde\xad", reports=v1_stream)
        self.addCleanup(dev.shutdown)
        base = DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")
        tp = TestPage({base.id: base}, enumerate_fn=lambda b: [new], open_fn=lambda i, s: dev,
                      probe_kwargs=dict(auto_s=0.5, user_s=0.5, grace_s=0, use_user=False))
        self.addCleanup(tp.shutdown)
        tp.start_probe(new.path, False)
        gen = tp.gen
        tp.auto_tick()                                                   # автовиявлення не лізе, поки йде пошук
        self.assertIsNone(tp.probe)
        self.assertEqual(tp.gen, gen)


if __name__ == "__main__":
    unittest.main()
