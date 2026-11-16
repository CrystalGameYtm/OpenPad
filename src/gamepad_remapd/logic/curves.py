"""Response curves as lookup tables (LUT).

Idea: all the expensive math (pow/exp/interpolation between points) is done ONCE
during profile compilation; the hot path keeps only a list index and one linear
interpolation. A curve is a function f: [0,1] -> [0,1].
"""
from __future__ import annotations

import math
from typing import Any, Dict, List
from ..i18n import tr



LUT_N = 1024
LUT_SIZE = LUT_N + 2

CURVE_TYPES = ("linear", "power", "smooth", "expo", "points")


class CurveError(ValueError):
    """Invalid curve description in a profile."""


def validate_curve(spec: Any, path: str = "curve") -> Dict[str, Any]:
    """Validates a curve description and returns its normalized copy."""
    if spec is None:
        return {"type": "linear"}
    if not isinstance(spec, dict):
        raise CurveError(tr('{0}: expected an object, got {1}', path, type(spec).__name__))
    ctype = spec.get("type", "linear")
    if ctype not in CURVE_TYPES:
        raise CurveError(tr('{0}.type: {1!r} is not one of {2}', path, ctype, CURVE_TYPES))
    out: Dict[str, Any] = {"type": ctype}
    if ctype == "power":
        e = spec.get("exponent", 1.0)
        if not isinstance(e, (int, float)) or isinstance(e, bool) or not 0.1 <= e <= 10:
            raise CurveError(tr('{0}.exponent: a number 0.1..10 is required', path))
        out["exponent"] = float(e)
    elif ctype == "smooth":
        s = spec.get("strength", 1.0)
        if not isinstance(s, (int, float)) or isinstance(s, bool) or not 0 <= s <= 1:
            raise CurveError(tr('{0}.strength: a number 0..1 is required', path))
        out["strength"] = float(s)
    elif ctype == "expo":
        k = spec.get("k", 3.0)
        if not isinstance(k, (int, float)) or isinstance(k, bool) or not -20 <= k <= 20 or k == 0:
            raise CurveError(tr('{0}.k: a non-zero number -20..20 is required', path))
        out["k"] = float(k)
    elif ctype == "points":
        pts = spec.get("points")
        if not isinstance(pts, list) or len(pts) < 2:
            raise CurveError(tr('{0}.points: a list of at least 2 points [x, y] is required', path))
        norm: List[List[float]] = []
        for i, p in enumerate(pts):
            if (not isinstance(p, (list, tuple)) or len(p) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
                raise CurveError(tr('{0}.points[{1}]: expected [x, y]', path, i))
            x, y = float(p[0]), float(p[1])
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise CurveError(tr('{0}.points[{1}]: x and y must be in 0..1', path, i))
            norm.append([x, y])
        xs = [p[0] for p in norm]
        if xs != sorted(xs) or len(set(xs)) != len(xs):
            raise CurveError(tr('{0}.points: x must be strictly increasing', path))
        out["points"] = norm
    return out


def _curve_fn(spec: Dict[str, Any]):
    """Returns a Python function f(t) for the curve description (called only while building
    the LUT, so speed does not matter here).
    """
    ctype = spec["type"]
    if ctype == "linear":
        return lambda t: t
    if ctype == "power":
        e = spec["exponent"]
        return lambda t: t ** e
    if ctype == "smooth":
        s = spec["strength"]
        
        return lambda t: (1 - s) * t + s * (t * t * (3 - 2 * t))
    if ctype == "expo":
        k = spec["k"]
        denom = math.expm1(k)
        return lambda t: math.expm1(k * t) / denom
    pts = spec["points"]
    
    def f(t: float) -> float:
        if t <= pts[0][0]:
            return pts[0][1]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if t <= x1:
                return y0 + (y1 - y0) * (t - x0) / (x1 - x0)
        return pts[-1][1]
    return f


def build_curve_lut(spec: Dict[str, Any] | None) -> List[float]:
    """Builds a LUT (LUT_SIZE values) for the curve. Values are clamped to [0,1]."""
    fn = _curve_fn(validate_curve(spec))
    lut = []
    for i in range(LUT_N + 1):
        v = fn(i / LUT_N)
        lut.append(0.0 if v < 0.0 else 1.0 if v > 1.0 else v)
    lut.append(lut[-1])  
    return lut


def lut_eval(lut: List[float], t: float) -> float:
    """Evaluates the curve through the LUT for t∈[0,1] with linear interpolation.

        The hot path inlines these same three lines; this function is for tests/cold code.
    """
    f = t * LUT_N
    i = int(f)
    return lut[i] + (lut[i + 1] - lut[i]) * (f - i)
