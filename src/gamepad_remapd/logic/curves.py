"""Криві чутливості (response curves) у вигляді таблиць пошуку (LUT).

Ідея: усю «дорогу» математику (pow/exp/інтерполяція за точками) робимо ОДИН
раз під час компіляції профілю, а на гарячому шляху залишається лише індексація
списку та одна лінійна інтерполяція. Крива — це функція f: [0,1] -> [0,1].
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

# Кількість інтервалів LUT. Список має LUT_N + 2 елементів: два «запасні» в
# кінці, щоб лінійна інтерполяція lut[i+1] ніколи не виходила за межі при t == 1.
LUT_N = 1024
LUT_SIZE = LUT_N + 2

CURVE_TYPES = ("linear", "power", "smooth", "expo", "points")


class CurveError(ValueError):
    """Некоректний опис кривої у профілі."""


def validate_curve(spec: Any, path: str = "curve") -> Dict[str, Any]:
    """Перевіряє опис кривої та повертає його нормалізовану копію."""
    if spec is None:
        return {"type": "linear"}
    if not isinstance(spec, dict):
        raise CurveError(f"{path}: очікується об'єкт, отримано {type(spec).__name__}")
    ctype = spec.get("type", "linear")
    if ctype not in CURVE_TYPES:
        raise CurveError(f"{path}.type: {ctype!r} не з {CURVE_TYPES}")
    out: Dict[str, Any] = {"type": ctype}
    if ctype == "power":
        e = spec.get("exponent", 1.0)
        if not isinstance(e, (int, float)) or isinstance(e, bool) or not 0.1 <= e <= 10:
            raise CurveError(f"{path}.exponent: потрібне число 0.1..10")
        out["exponent"] = float(e)
    elif ctype == "smooth":
        s = spec.get("strength", 1.0)
        if not isinstance(s, (int, float)) or isinstance(s, bool) or not 0 <= s <= 1:
            raise CurveError(f"{path}.strength: потрібне число 0..1")
        out["strength"] = float(s)
    elif ctype == "expo":
        k = spec.get("k", 3.0)
        if not isinstance(k, (int, float)) or isinstance(k, bool) or not -20 <= k <= 20 or k == 0:
            raise CurveError(f"{path}.k: потрібне ненульове число -20..20")
        out["k"] = float(k)
    elif ctype == "points":
        pts = spec.get("points")
        if not isinstance(pts, list) or len(pts) < 2:
            raise CurveError(f"{path}.points: потрібен список щонайменше з 2 точок [x, y]")
        norm: List[List[float]] = []
        for i, p in enumerate(pts):
            if (not isinstance(p, (list, tuple)) or len(p) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
                raise CurveError(f"{path}.points[{i}]: очікується [x, y]")
            x, y = float(p[0]), float(p[1])
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise CurveError(f"{path}.points[{i}]: x та y мають бути в 0..1")
            norm.append([x, y])
        xs = [p[0] for p in norm]
        if xs != sorted(xs) or len(set(xs)) != len(xs):
            raise CurveError(f"{path}.points: x мають строго зростати")
        out["points"] = norm
    return out


def _curve_fn(spec: Dict[str, Any]):
    """Повертає python-функцію f(t) для опису кривої (викликається лише при
    побудові LUT, тому швидкодія тут неважлива)."""
    ctype = spec["type"]
    if ctype == "linear":
        return lambda t: t
    if ctype == "power":
        e = spec["exponent"]
        return lambda t: t ** e
    if ctype == "smooth":
        s = spec["strength"]
        # змішування лінійної та smoothstep: 0 -> лінійна, 1 -> повний S-подіб
        return lambda t: (1 - s) * t + s * (t * t * (3 - 2 * t))
    if ctype == "expo":
        k = spec["k"]
        denom = math.expm1(k)
        return lambda t: math.expm1(k * t) / denom
    pts = spec["points"]
    # Кусково-лінійна інтерполяція; ззовні [x0, xn] — константа крайніх y.
    def f(t: float) -> float:
        if t <= pts[0][0]:
            return pts[0][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if t <= x1:
                return y0 + (y1 - y0) * (t - x0) / (x1 - x0)
        return pts[-1][1]
    return f


def build_curve_lut(spec: Dict[str, Any] | None) -> List[float]:
    """Будує LUT (LUT_SIZE значень) для кривої. Значення затискаються в [0,1]."""
    fn = _curve_fn(validate_curve(spec))
    lut = []
    for i in range(LUT_N + 1):
        v = fn(i / LUT_N)
        lut.append(0.0 if v < 0.0 else 1.0 if v > 1.0 else v)
    lut.append(lut[-1])  # запасний елемент для інтерполяції при t == 1.0
    return lut


def lut_eval(lut: List[float], t: float) -> float:
    """Обчислює криву за LUT для t∈[0,1] з лінійною інтерполяцією.

    Гарячий шлях інлайнить ці самі три рядки; ця функція — для тестів/холодного коду.
    """
    f = t * LUT_N
    i = int(f)
    return lut[i] + (lut[i + 1] - lut[i]) * (f - i)
