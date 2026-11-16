"""Gyroscope zero calibration (bias): the gamepad lies still -> the average of the raw values = the zero offset.

Without this, a resting sensor gives a constant "speed" (tens of units), and gyro aiming drifts slowly. A pure function,
with no device dependencies — easy to test.
"""
from __future__ import annotations

from statistics import fmean, pstdev
from typing import Dict, Sequence
from ..i18n import tr


class GyroCalError(ValueError):
    """Calibration is impossible (too little data, or the gamepad moved)."""


def estimate_bias(yaw: Sequence[float], pitch: Sequence[float], dps_per_lsb: float,
                  max_noise_dps: float = 3.0, max_drift_dps: float = 2.0, min_samples: int = 40) -> Dict[str, object]:
    n = min(len(yaw), len(pitch))
    if n < min_samples:
        raise GyroCalError(tr('too little data ({0} reports, at least {1} needed): the device is silent or sends reports too slowly', n, min_samples))
    yaw, pitch = list(yaw)[:n], list(pitch)[:n]
    sc = abs(dps_per_lsb)
    noise = max(pstdev(yaw), pstdev(pitch)) * sc
    h = n // 2
    drift = max(abs(fmean(yaw[:h]) - fmean(yaw[h:])), abs(fmean(pitch[:h]) - fmean(pitch[h:]))) * sc
    if noise > max_noise_dps or drift > max_drift_dps:
        raise GyroCalError(tr('the gamepad was moving (noise {0:.1f} °/s, drift {1:.1f} °/s): put it on a table and repeat', noise, drift))
    return {"bias": [round(fmean(yaw), 2), round(fmean(pitch), 2)], "samples": n, "noise_dps": round(noise, 2)}
