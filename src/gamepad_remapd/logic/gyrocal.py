"""Калібрування нуля гіроскопа (bias): геймпад лежить нерухомо -> середнє сирих значень = зміщення нуля.

Без цього датчик у спокої дає сталу «швидкість» (десятки одиниць), і гіроприціл повільно дрейфує. Чиста функція,
без залежностей від пристрою — легко тестується.
"""
from __future__ import annotations

from statistics import fmean, pstdev
from typing import Dict, Sequence


class GyroCalError(ValueError):
    """Калібрування неможливе (мало даних або геймпад рухався)."""


def estimate_bias(yaw: Sequence[float], pitch: Sequence[float], dps_per_lsb: float,
                  max_noise_dps: float = 3.0, max_drift_dps: float = 2.0, min_samples: int = 40) -> Dict[str, object]:
    n = min(len(yaw), len(pitch))
    if n < min_samples:
        raise GyroCalError(f"замало даних ({n} звітів, потрібно ≥ {min_samples}): пристрій мовчить або надто повільно шле звіти")
    yaw, pitch = list(yaw)[:n], list(pitch)[:n]
    sc = abs(dps_per_lsb)
    noise = max(pstdev(yaw), pstdev(pitch)) * sc
    h = n // 2
    drift = max(abs(fmean(yaw[:h]) - fmean(yaw[h:])), abs(fmean(pitch[:h]) - fmean(pitch[h:]))) * sc
    if noise > max_noise_dps or drift > max_drift_dps:
        raise GyroCalError(f"геймпад рухався (шум {noise:.1f} °/с, дрейф {drift:.1f} °/с): покладіть його на стіл і повторіть")
    return {"bias": [round(fmean(yaw), 2), round(fmean(pitch), 2)], "samples": n, "noise_dps": round(noise, 2)}
