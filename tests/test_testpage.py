"""Тестова веб-сторінка: справжній HTTP-сервер + SSE на фейковому пристрої."""
from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from pathlib import Path

from gamepad_remapd.logic.layout import DeviceLayout
from gamepad_remapd.tools.testpage import TestPage, make_server

from .fakes import FakeDevice
from .test_autolayout import v1_report

LAYOUTS = Path(__file__).resolve().parent.parent / "src" / "gamepad_remapd" / "layouts"
from gamepad_remapd.input.base import DeviceInfo

INFO = DeviceInfo(path="/dev/hidraw10", vid=0x04B4, pid=0x2412, name="Flydigi", interface=2, backend="hidraw")


class TestPageE2E(unittest.TestCase):
    def setUp(self):
        lay = DeviceLayout.from_file(LAYOUTS / "flydigi_v1_2g4.json")
        self.dev = FakeDevice(INFO)
        self.tp = TestPage({lay.id: lay}, enumerate_fn=lambda b: [INFO], open_fn=lambda i, s: self.dev)
        self.srv = make_server(self.tp, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.tp.close()
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def test_static_files_and_csp(self):
        for path, ctype in (("/", "text/html"), ("/testpage.js", "text/javascript"), ("/testpage.css", "text/css")):
            st, data, r = self.req("GET", path)
            self.assertEqual(st, 200, path)
            self.assertTrue(r.getheader("Content-Type").startswith(ctype))
            self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))
            self.assertGreater(len(data), 100)
        self.assertNotIn(b"style=", self.req("GET", "/")[1])        # CSP: жодних inline-стилів
        self.assertNotIn(b"<script>", self.req("GET", "/")[1])

    def test_host_and_origin_guard(self):
        st, _, _ = self.req("GET", "/api/devices", headers={"Host": "evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/api/open", {"path": INFO.path},
                            headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertIsNone(self.tp.probe)

    def test_devices_list_and_open_errors(self):
        st, data, _ = self.req("GET", "/api/devices")
        d = json.loads(data)
        self.assertEqual(d["devices"][0]["layout"], "flydigi_v1_2g4")
        self.assertEqual(self.req("POST", "/api/open", {"path": "/dev/nope"})[0], 404)
        self.assertEqual(self.req("POST", "/api/open", {"path": INFO.path, "layout": "x"})[0], 400)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/open", "[1]", {"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def _read_sse(self, until, timeout=5.0):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        c.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        out, ev, deadline = [], None, time.time() + timeout
        while time.time() < deadline:
            line = r.fp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                msg = (ev, json.loads(line[6:]))
                out.append(msg)
                if until(msg):
                    break
        c.close()
        return out

    def test_open_streams_state_events_and_runs_handshake(self):
        st, data, _ = self.req("POST", "/api/open", {"path": INFO.path})
        self.assertEqual(st, 200)
        desc = json.loads(data)
        self.assertEqual(desc["layout"], "flydigi_v1_2g4")
        names = {b["name"]: b["target"] for b in desc["buttons"]}
        self.assertEqual((names["M1"], names["EXTRA1"], names["PAD_UP"]), ("M1", "C", "UP"))
        self.assertEqual(desc["triggers"], {"left": "L2", "right": "R2"})
        self.assertIn(9, desc["covered_bytes"])
        self.assertNotIn(12, desc["covered_bytes"])          # IMU не покрито розкладкою
        # handshake відправлено до першого звіту
        for _ in range(100):
            if self.dev.outputs:
                break
            time.sleep(0.02)
        self.assertEqual(self.dev.outputs[0], bytes.fromhex("05ec") + bytes(10))

        def feed():
            time.sleep(0.2)
            self.dev.inject(bytes.fromhex("04ff f05f") + bytes(28))         # відповідь handshake: відсіюється
            self.dev.inject(v1_report())
            self.dev.inject(v1_report(b9=0x10, lx=255, r2=200))              # A + LX max + R2
            self.dev.inject(v1_report(lx=255, r2=200))                       # A відпущено
        threading.Thread(target=feed, daemon=True).start()
        seen = {"events": []}

        def done(msg):
            if msg[0] == "state":
                seen["events"] += msg[1]["events"]
                seen["last"] = msg[1]
            return any(not e["down"] for e in seen["events"])

        self._read_sse(done)
        evs = [(e["name"], e["target"], e["down"]) for e in seen["events"]]
        self.assertEqual(evs, [("A", "A", True), ("A", "A", False)])      # події не губляться навіть при коалесингу
        last = seen["last"]
        self.assertEqual(last["status"]["state"], "running")
        self.assertEqual(last["axes"]["LX"], 255)
        self.assertEqual(last["axes"]["R2"], 200)
        self.assertEqual(last["len"], 32)

    def test_current_and_close(self):
        self.assertEqual(json.loads(self.req("GET", "/api/current")[1]), {})
        self.req("POST", "/api/open", {"path": INFO.path})
        cur = json.loads(self.req("GET", "/api/current")[1])
        self.assertEqual(cur["layout"], "flydigi_v1_2g4")
        self.assertEqual(self.req("POST", "/api/close", {})[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/current")[1]), {})
        self.assertTrue(self.dev.closed)

    def test_device_gone_is_reported(self):
        self.req("POST", "/api/open", {"path": INFO.path})
        threading.Thread(target=lambda: (time.sleep(0.2), self.dev.inject(v1_report()),
                                         self.dev.unplug()), daemon=True).start()
        out = self._read_sse(lambda m: m[0] == "state" and m[1]["status"]["state"] == "gone")
        self.assertEqual(out[-1][1]["status"]["state"], "gone")

    def test_open_permission_error_is_reported(self):
        def boom(i, s):
            raise PermissionError(13, "Permission denied")
        self.tp.open_fn = boom
        self.req("POST", "/api/open", {"path": INFO.path})
        out = self._read_sse(lambda m: m[0] == "state" and m[1]["status"]["state"] == "error")
        self.assertIn("немає прав", out[-1][1]["status"]["message"])


if __name__ == "__main__":
    unittest.main()


class CalibrationPageTests(unittest.TestCase):
    """Майстер калібрування через HTTP: старт -> кроки (скрипт-«користувач») -> збереження -> автовиявлення."""

    def test_wizard_over_http_and_save(self):
        import tempfile
        from . import calib_sim as sim
        from .test_calibrate_job import SimDevice
        info = DeviceInfo(path="/dev/hidraw9", vid=0x1234, pid=0x5678, name="Sim", interface=0, bus=3, backend="hidraw")
        ref = [None]
        dev = SimDevice(info, sim.XBOX360_USB, ref, absent=("M1", "M2", "M3", "M4", "C", "Z"))
        with tempfile.TemporaryDirectory() as d:
            tp = TestPage({}, enumerate_fn=lambda b: [info], open_fn=lambda i, s: dev, auto=False, save_dir=Path(d))
            tp.calib_kwargs = {"baseline_s": 0.3, "listen_s": 0.3}
            srv = make_server(tp, "127.0.0.1", 0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()

            def req(method, path, body=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request(method, path, json.dumps(body) if body is not None else None, {"Host": f"127.0.0.1:{port}"})
                r = c.getresponse()
                data = json.loads(r.read() or b"{}")
                c.close()
                return r.status, data
            try:
                self.assertEqual(req("GET", "/api/calib")[1]["state"], "idle")
                self.assertEqual(req("POST", "/api/calib", {"path": "/dev/nope"})[0], 404)
                self.assertEqual(req("POST", "/api/calib/skip", {})[0], 400)          # ще не запущено
                st, body = req("POST", "/api/calib", {"path": info.path})
                self.assertEqual(st, 200, body)
                ref[0] = tp._calib
                self.assertEqual(req("POST", "/api/calib", {"path": info.path})[0], 400)  # другий запуск
                end = time.monotonic() + 40
                while time.monotonic() < end and req("GET", "/api/calib")[1].get("active"):
                    time.sleep(0.1)
                st = req("GET", "/api/calib")[1]
                self.assertEqual(st["state"], "done", st)
                self.assertGreater(len(st["done"]), 20)
                st, saved = req("POST", "/api/calib/save", {})
                self.assertEqual(st, 200, saved)
                self.assertTrue(Path(saved["saved"]).exists())
                self.assertIn(saved["layout"], tp.layouts)                            # одразу доступна
                self.assertTrue(tp.auto)
            finally:
                tp.shutdown()
                srv.shutdown()
                srv.server_close()
