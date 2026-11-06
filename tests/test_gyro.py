import asyncio
import tempfile
import threading
import time
import unittest

from gamepad_remapd.logic.gyrocal import GyroCalError, estimate_bias
from gamepad_remapd.ipc.client import ControlError

from .fakes import ds4_report
from .test_integration import Rig


class GyroCalTests(unittest.TestCase):
    def test_bias_ok(self):
        r = estimate_bias([30, 31, 29, 30] * 20, [-12, -11, -13, -12] * 20, 0.061)
        self.assertEqual(r["bias"], [30.0, -12.0])

    def test_moving_rejected(self):
        ys = [i * 40 for i in range(80)]            # рівномірний поворот
        with self.assertRaises(GyroCalError):
            estimate_bias(ys, [0] * 80, 0.061)

    def test_noisy_rejected(self):
        with self.assertRaises(GyroCalError):
            estimate_bias([0, 3000] * 40, [0] * 80, 0.061)

    def test_too_few(self):
        with self.assertRaises(GyroCalError):
            estimate_bias([1] * 5, [1] * 5, 0.061)


class GyroIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.rig = Rig(self._td.name)
        await self.rig.start()

    async def asyncTearDown(self):
        await self.rig.stop()
        self._td.cleanup()

    def _feed(self, dev, gx, gy, stop, jitter=0):
        n = 0
        while not stop.is_set():
            n += 1
            dev.inject(ds4_report(gyro_x=gx + (jitter if n % 2 else -jitter), gyro_y=gy))
            time.sleep(0.004)

    async def test_snapshot_and_calibration(self):
        dev = await self.rig.plug()
        stop = threading.Event()
        t = threading.Thread(target=self._feed, args=(dev, 100, 500, stop), daemon=True)
        t.start()
        try:
            await self.rig.wait_for(lambda: any(e["event"] == "state" and e.get("gyro") for e in self.rig.events))
            g = [e for e in self.rig.events if e["event"] == "state" and e.get("gyro")][-1]["gyro"]
            self.assertEqual(g[0], round(500 * 0.061))          # yaw = GYRO_Y
            self.assertEqual(g[1], round(100 * 0.061))          # pitch = GYRO_X
            r = await self.rig.client.call("calibrate_gyro", device="hidraw7", duration=0.6)
            self.assertEqual(r["bias"], [500.0, 100.0])
            self.assertEqual(r["data"]["gyro"]["bias"], [500.0, 100.0])
        finally:
            stop.set()
            t.join()

    async def test_calibration_refused_when_moving(self):
        dev = await self.rig.plug()
        stop = threading.Event()
        t = threading.Thread(target=self._feed, args=(dev, 0, 0, stop, 4000), daemon=True)
        t.start()
        try:
            with self.assertRaises(ControlError):
                await self.rig.client.call("calibrate_gyro", device="hidraw7", duration=0.6)
        finally:
            stop.set()
            t.join()


if __name__ == "__main__":
    unittest.main()
