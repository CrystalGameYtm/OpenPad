"""User profiles: JSON validation + compilation into hot-path structures.

Two stages:
  1. `normalize_profile(dict)` — checks the shape and ranges, fills in the defaults.
     Returns a clean JSON-compatible dict. The JSON merge-patch (RFC 7386) of hot
     settings changes works on this dict.
  2. `compile_profile(...)` — turns the dict + the device layout + the backend into a
     `CompiledProfile`: everything is precomputed (LUTs, scales, bit tables), and the
     hot path only reads these structures. Compilation ALWAYS happens outside the hot
     thread; the thread picks up the new profile by an atomic reference assignment
     (`pipeline.cp = new_cp`).
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..constants import ALL_TARGETS, ANALOG_BUTTON_TARGETS
from .keytargets import key_target_code
from ..output.base import OutputBackend
from .curves import CurveError, LUT_N, build_curve_lut, validate_curve
from .layout import (DeviceLayout, FieldDef, HAT_DEFAULT_TARGETS, ReportParser)
from ..i18n import tr

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ProfileError(ValueError):
    """Invalid profile (the message contains the path to the faulty field)."""


# --------------------------------------------------------------------------
# 1. Normalization
# --------------------------------------------------------------------------
def _num(d: dict, key: str, default: float, lo: float, hi: float, path: str) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        raise ProfileError(tr('{0}.{1}: a number is expected', path, key))
    if not lo <= v <= hi:
        raise ProfileError(tr('{0}.{1}: {2} is out of range {3}..{4}', path, key, v, lo, hi))
    return float(v)


def _bool(d: dict, key: str, default: bool, path: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ProfileError(tr('{0}.{1}: true/false is expected', path, key))
    return v


def _reject_unknown(d: dict, allowed: Tuple[str, ...], path: str) -> None:
    extra = [k for k in d if k not in allowed and not k.startswith("_")]
    if extra:
        raise ProfileError(tr('{0}: unknown keys {1}; allowed {2}', path, extra, list(allowed)))


def _curve(d: dict, path: str) -> dict:
    try:
        return validate_curve(d.get("curve"), f"{path}.curve")
    except CurveError as e:
        raise ProfileError(str(e)) from None


def _stick(d: Any, path: str) -> dict:
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise ProfileError(tr('{0}: an object is expected', path))
    _reject_unknown(d, ("inner_deadzone", "outer_deadzone", "shape", "curve",
                        "sensitivity", "invert_x", "invert_y", "swap_xy"), path)
    inner = _num(d, "inner_deadzone", 0.05, 0.0, 0.95, path)
    outer = _num(d, "outer_deadzone", 1.0, 0.05, 1.0, path)
    if outer <= inner + 0.01:
        raise ProfileError(tr('{0}: outer_deadzone must be greater than inner_deadzone', path))
    shape = d.get("shape", "radial")
    if shape not in ("radial", "axial"):
        raise ProfileError(tr("{0}.shape: 'radial' or 'axial'", path))
    return {"inner_deadzone": inner, "outer_deadzone": outer, "shape": shape,
            "curve": _curve(d, path), "sensitivity": _num(d, "sensitivity", 1.0, 0.1, 4.0, path),
            "invert_x": _bool(d, "invert_x", False, path), "invert_y": _bool(d, "invert_y", False, path),
            "swap_xy": _bool(d, "swap_xy", False, path)}


TRIGGER_MODES = ("analog", "digital", "off")
STAGE_NAMES = ("soft", "full")


def _stages(d: Any, path: str) -> dict:
    """stages: {"soft": {"at": 0.3, "targets": [...]}, "full": {...}} — actions at stages of the trigger travel."""
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise ProfileError(tr('{0}: an object {{soft, full}} is expected', path))
    _reject_unknown(d, STAGE_NAMES, path)
    out: dict = {}
    for name, defat in (("soft", 0.3), ("full", 0.9)):
        st = d.get(name)
        if st is None:
            continue
        p2 = f"{path}.{name}"
        if not isinstance(st, dict):
            raise ProfileError(tr('{0}: an object {{at, targets}} is expected', p2))
        _reject_unknown(st, ("at", "targets"), p2)
        targets = _norm_targets(st.get("targets", []), f"{p2}.targets")
        if targets:
            out[name] = {"at": _num(st, "at", defat, 0.01, 1.0, p2), "targets": targets}
    if "soft" in out and "full" in out and out["full"]["at"] <= out["soft"]["at"] + 0.02:
        raise ProfileError(tr('{0}: full.at must be greater than soft.at', path))
    return out


def _trigger(d: Any, path: str) -> dict:
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise ProfileError(tr('{0}: an object is expected', path))
    _reject_unknown(d, ("inner_deadzone", "outer_deadzone", "curve", "sensitivity", "invert",
                        "mode", "threshold", "button_value", "stages"), path)
    inner = _num(d, "inner_deadzone", 0.0, 0.0, 0.95, path)
    outer = _num(d, "outer_deadzone", 1.0, 0.05, 1.0, path)
    if outer <= inner + 0.01:
        raise ProfileError(tr('{0}: outer_deadzone must be greater than inner_deadzone', path))
    mode = d.get("mode", "analog")
    if mode not in TRIGGER_MODES:
        raise ProfileError(tr('{0}.mode: one of {1}', path, list(TRIGGER_MODES)))
    return {"inner_deadzone": inner, "outer_deadzone": outer, "curve": _curve(d, path),
            "sensitivity": _num(d, "sensitivity", 1.0, 0.1, 4.0, path),
            "invert": _bool(d, "invert", False, path),
            "mode": mode, "threshold": _num(d, "threshold", 0.5, 0.01, 1.0, path),
            "button_value": _num(d, "button_value", 1.0, 0.0, 1.0, path),
            "stages": _stages(d.get("stages"), f"{path}.stages")}


def _gyro(d: Any, path: str) -> dict:
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise ProfileError(tr('{0}: an object is expected', path))
    _reject_unknown(d, ("enabled", "target", "activation", "deadzone_dps", "full_dps",
                        "curve", "invert_yaw", "invert_pitch", "smoothing", "bias", "mouse_sens"), path)
    target = d.get("target", "right_stick")
    if target not in ("right_stick", "left_stick", "mouse"):
        raise ProfileError(tr("{0}.target: 'right_stick', 'left_stick' or 'mouse'", path))
    act = d.get("activation")
    if act is not None and not isinstance(act, str):
        raise ProfileError(tr('{0}.activation: the name of an input button or null (always)', path))
    bias = d.get("bias", [0, 0])
    if (not isinstance(bias, list) or len(bias) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bias)):
        raise ProfileError(tr('{0}.bias: [yaw, pitch] in raw sensor units', path))
    return {"enabled": _bool(d, "enabled", False, path), "target": target, "activation": act,
            "deadzone_dps": _num(d, "deadzone_dps", 1.0, 0.0, 200.0, path),
            "full_dps": _num(d, "full_dps", 180.0, 5.0, 4000.0, path),
            "curve": _curve(d, path),
            "invert_yaw": _bool(d, "invert_yaw", False, path),
            "invert_pitch": _bool(d, "invert_pitch", False, path),
            "smoothing": _num(d, "smoothing", 0.0, 0.0, 0.95, path),
            "bias": [float(bias[0]), float(bias[1])],
            "mouse_sens": _num(d, "mouse_sens", 6.0, 0.1, 200.0, path)}


def _norm_targets(v: Any, path: str) -> List[str]:
    """button_map value -> list of target buttons ([] = button disabled)."""
    if v == "NONE" or v == []:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ProfileError(tr('{0}: a target name, a list of names, or [] / "NONE" (disable)', path))
    out: List[str] = []
    for x in v:
        if x in ALL_TARGETS:
            out.append(x)
        elif key_target_code(x) is not None:
            out.append(x.strip().upper())               # KEY_F, MOUSE_LEFT: case does not matter
        else:
            raise ProfileError(tr('{0}: unknown target {1!r}; available: {2}, keys KEY_<name> (e.g. KEY_F, KEY_SPACE, KEY_LEFTSHIFT) and MOUSE_LEFT/RIGHT/MIDDLE/SIDE/EXTRA', path, x, list(ALL_TARGETS)))
    return list(dict.fromkeys(out))  


def normalize_profile(raw: Any) -> Dict[str, Any]:
    """Checks the profile and returns a fully populated dict."""
    if not isinstance(raw, dict):
        raise ProfileError(tr('the profile must be a JSON object'))
    _reject_unknown(raw, ("name", "description", "device", "button_map", "sticks", "triggers",
                          "gyro", "swap_sticks", "swap_triggers"), "profile")
    name = raw.get("name", "unnamed")
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        raise ProfileError(tr('name: letters/digits/._- , up to 64 characters'))
    bm_raw = raw.get("button_map") or {}
    if not isinstance(bm_raw, dict):
        raise ProfileError(tr('button_map: an object {source: target} is expected'))
    sticks = raw.get("sticks") or {}
    triggers = raw.get("triggers") or {}
    out = {
        "name": name,
        "description": str(raw.get("description", "")),
        "device": raw.get("device"),
        "button_map": {str(k): _norm_targets(v, f"button_map.{k}") for k, v in bm_raw.items()},
        "sticks": {s: _stick(sticks.get(s), f"sticks.{s}") for s in ("left", "right")},
        "triggers": {s: _trigger(triggers.get(s), f"triggers.{s}") for s in ("left", "right")},
        "gyro": _gyro(raw.get("gyro"), "gyro"),
        "swap_sticks": _bool(raw, "swap_sticks", False, "profile"),
        "swap_triggers": _bool(raw, "swap_triggers", False, "profile"),
    }
    for side in sticks:
        if side not in ("left", "right"):
            raise ProfileError(tr('sticks.{0}: only left/right', side))
    for side in triggers:
        if side not in ("left", "right"):
            raise ProfileError(tr('triggers.{0}: only left/right', side))
    if out["device"] is not None and not isinstance(out["device"], str):
        raise ProfileError(tr('device: a layout id or null'))
    return out


def merge_patch(target: Any, patch: Any) -> Any:
    """JSON Merge Patch (RFC 7386): null removes a key, objects are merged recursively."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            result.pop(k, None)
        else:
            result[k] = merge_patch(result.get(k), v)
    return result


# --------------------------------------------------------------------------
# 2. Compilation: the expensive work is done once, outside the hot path
# --------------------------------------------------------------------------
class StickProc:
    """Processes one stick: center -> normalization -> deadzone -> curve -> scale.

        Returns (x, y) as floats in [-1, 1] (evdev convention: Y up = minus).
        Caches the last input: when the stick does not move (the most common case),
        it returns the ready result without any math.
    """
    __slots__ = ("ix", "iy", "cx", "cy", "kx", "ky", "inner", "inner2", "inv_span", "lut",
                 "radial", "sens", "inv_x", "inv_y", "swap", "_last_in", "_last_out")

    def __init__(self, ix: int, iy: int, fx: FieldDef, fy: FieldDef, cfg: dict):
        self.ix, self.iy = ix, iy
        self.cx, self.cy = fx.center, fy.center
        # kx/ky = 1/half-range (sign: the layout `invert` field for axes where "up = plus")
        self.kx = (-1.0 if fx.invert else 1.0) / fx.half_range
        self.ky = (-1.0 if fy.invert else 1.0) / fy.half_range
        self.inner = cfg["inner_deadzone"]
        self.inner2 = self.inner * self.inner
        self.inv_span = 1.0 / (cfg["outer_deadzone"] - self.inner)
        self.lut = build_curve_lut(cfg["curve"])
        self.radial = cfg["shape"] == "radial"
        self.sens = cfg["sensitivity"]
        self.inv_x, self.inv_y, self.swap = cfg["invert_x"], cfg["invert_y"], cfg["swap_xy"]
        self._last_in: Optional[Tuple[int, int]] = None
        self._last_out = (0.0, 0.0)

    def apply(self, vals: tuple) -> Tuple[float, float]:
        vx = vals[self.ix]
        vy = vals[self.iy]
        last = self._last_in
        if last is not None and last[0] == vx and last[1] == vy:
            return self._last_out
        x = (vx - self.cx) * self.kx
        y = (vy - self.cy) * self.ky
        if self.swap:
            x, y = y, x
        if self.radial:
            m2 = x * x + y * y
            if m2 <= self.inner2:
                res = (0.0, 0.0)  
            else:
                m = math.sqrt(m2)
                if m > 1.0:  # "square" sticks: clip to the unit circle
                    x /= m
                    y /= m
                    m = 1.0
                t = (m - self.inner) * self.inv_span  
                if t > 1.0:
                    t = 1.0
                f = t * LUT_N                          
                i = int(f)
                lut = self.lut
                c = (lut[i] + (lut[i + 1] - lut[i]) * (f - i)) * self.sens
                if c > 1.0:
                    c = 1.0
                s = c / m                              
                res = (x * s, y * s)
        else:  # axial: each axis separately
            res = (self._axial(x), self._axial(y))
        if self.inv_x:
            res = (-res[0], res[1])
        if self.inv_y:
            res = (res[0], -res[1])
        self._last_in = (vx, vy)
        self._last_out = res
        return res

    def _axial(self, v: float) -> float:
        a = -v if v < 0 else v
        if a <= self.inner:
            return 0.0
        if a > 1.0:
            a = 1.0
        t = (a - self.inner) * self.inv_span
        if t > 1.0:
            t = 1.0
        f = t * LUT_N
        i = int(f)
        lut = self.lut
        c = (lut[i] + (lut[i + 1] - lut[i]) * (f - i)) * self.sens
        if c > 1.0:
            c = 1.0
        return -c if v < 0 else c


class TriggerProc:
    """Trigger: raw value -> LUT (inversion, deadzone, curve, sensitivity) -> level 0..255.

        `apply` returns the LEVEL (analog, after processing); what goes to the output is
        given by `out[level]` (mode analog / digital / off). Stages — thresholds on the
        level with hysteresis; their targets are pressed by the pipeline.
        The LUT has an entry for EVERY possible raw value: the hot path is an index lookup.
    """
    __slots__ = ("idx", "rmin", "lut", "out", "stage_at", "press")

    def __init__(self, idx: int, f: FieldDef, cfg: dict):
        self.idx, self.rmin = idx, f.raw_min
        curve = build_curve_lut(cfg["curve"])
        inner, outer = cfg["inner_deadzone"], cfg["outer_deadzone"]
        sens, inv_axis = cfg["sensitivity"], cfg["invert"]
        span = float(f.raw_max - f.raw_min)
        inv = 1.0 / (outer - inner)
        lut: List[int] = []
        for v in range(f.raw_min, f.raw_max + 1):
            n = (v - f.raw_min) / span
            if inv_axis:
                n = 1.0 - n
            if n <= inner:
                lut.append(0)
                continue
            t = min(1.0, (n - inner) * inv)
            ff = t * LUT_N
            i = int(ff)
            c = (curve[i] + (curve[i + 1] - curve[i]) * (ff - i)) * sens
            lut.append(255 if c >= 1.0 else int(c * 255.0 + 0.5))
        self.lut = lut
        mode = cfg["mode"]
        thr = max(1, int(cfg["threshold"] * 255.0 + 0.5))
        if mode == "digital":
            self.out = [255 if lv >= thr else 0 for lv in range(256)]
        elif mode == "off":
            self.out = [0] * 256
        else:
            self.out = list(range(256))
        # (threshold 1..255, stage name), ascending
        self.stage_at = tuple((max(1, int(cfg["stages"][n]["at"] * 255.0 + 0.5)), n)
                              for n in STAGE_NAMES if n in cfg["stages"])
        self.press = int(cfg["button_value"] * 255.0 + 0.5)

    def apply(self, vals: tuple) -> int:
        try:
            return self.lut[vals[self.idx] - self.rmin]
        except IndexError:  
            return self.lut[-1] if vals[self.idx] > self.rmin else 0


class GyroProc:
    """Gyro -> an additional stick deflection vector (aim assist "gyro aiming").

        yaw -> X axis, pitch -> Y axis. The rotation speed (°/s), after the dead zone and
        the curve, is normalized by `full_dps` (the speed that gives full deflection).
        The signs depend on the axes of the specific sensor — adjusted via invert_yaw/pitch.
    """
    __slots__ = ("iyaw", "ipitch", "scale", "dz", "inv_full", "lut", "sx", "sy",
                 "bias_yaw", "bias_pitch", "alpha", "act_mask", "to_right", "_fx", "_fy",
                 "to_mouse", "msens", "_mx", "_my", "_t")

    def __init__(self, cfg: dict, layout: DeviceLayout, parser: ReportParser,
                 act_mask: int):
        g = layout.gyro
        self.iyaw = parser.index[g["yaw"]]
        self.ipitch = parser.index[g["pitch"]]
        self.scale = float(g["dps_per_lsb"])
        self.dz = cfg["deadzone_dps"]
        self.inv_full = 1.0 / cfg["full_dps"]
        self.lut = build_curve_lut(cfg["curve"])
        self.sx = -1.0 if cfg["invert_yaw"] else 1.0
        self.sy = -1.0 if cfg["invert_pitch"] else 1.0
        self.bias_yaw, self.bias_pitch = cfg["bias"]
        self.alpha = 1.0 - cfg["smoothing"]  
        self.act_mask = act_mask
        self.to_right = cfg["target"] == "right_stick"
        self._fx = self._fy = 0.0
        self.to_mouse = cfg["target"] == "mouse"
        self.msens = float(cfg.get("mouse_sens", 6.0))      
        self._mx = self._my = 0.0                            
        self._t = 0                                          

    def mouse(self, vals: tuple, src: int, now_ns: int) -> Tuple[int, int]:
        """Gyro -> relative mouse motion (dx, dy in whole pixels). Integrates °/s * dt; the fractional
        part is accumulated, so slow movements are not lost. Up/right motion: yaw -> X, pitch -> Y (up = minus).
        """
        dt = (now_ns - self._t) * 1e-9 if self._t else 0.0
        self._t = now_ns
        if dt > 0.05:
            dt = 0.0                                         
        if self.act_mask and not (src & self.act_mask):
            self._mx = self._my = 0.0
            return (0, 0)
        sc = self.scale
        wy = (vals[self.iyaw] - self.bias_yaw) * sc * self.sx
        wp = (vals[self.ipitch] - self.bias_pitch) * sc * self.sy
        if -self.dz <= wy <= self.dz:
            wy = 0.0
        if -self.dz <= wp <= self.dz:
            wp = 0.0
        self._mx += wy * self.msens * dt
        self._my -= wp * self.msens * dt
        dx, dy = int(self._mx), int(self._my)
        self._mx -= dx
        self._my -= dy
        return (dx, dy)

    def _axis(self, raw: float, sign: float) -> float:
        d = raw * self.scale  # °/s
        a = -d if d < 0 else d
        if a <= self.dz:
            return 0.0
        t = (a - self.dz) * self.inv_full
        if t > 1.0:
            t = 1.0
        f = t * LUT_N
        i = int(f)
        lut = self.lut
        c = lut[i] + (lut[i + 1] - lut[i]) * (f - i)
        return sign * (c if d > 0 else -c)

    def apply(self, vals: tuple, src: int) -> Tuple[float, float]:
        if self.act_mask and not (src & self.act_mask):
            self._fx = self._fy = 0.0  
            return (0.0, 0.0)
        x = self._axis(vals[self.iyaw] - self.bias_yaw, self.sx)
        y = self._axis(vals[self.ipitch] - self.bias_pitch, self.sy)
        a = self.alpha
        if a < 1.0:  
            self._fx += a * (x - self._fx)
            self._fy += a * (y - self._fy)
            return (self._fx, self._fy)
        return (x, y)


@dataclass
class CompiledProfile:
    name: str
    data: Dict[str, Any]
    parser: ReportParser
    layout: DeviceLayout
    n_buttons: int                       
    bit_targets: List[Optional[Tuple[int, ...]]]  
    left: Optional[StickProc]
    right: Optional[StickProc]
    trig_l: Optional[TriggerProc]
    trig_r: Optional[TriggerProc]
    gyro: Optional[GyroProc]
    swap_sticks: bool
    swap_triggers: bool
    axis_ids: Tuple[int, int, int, int, int, int]  # LX, LY, RX, RY, LT, RT
    target_names: List[str]              # target id -> name (for the UI)
    warnings: List[str]
    stages_l: Tuple[Tuple[int, Tuple[int, ...]], ...] = ()  # (level threshold 1..255, target ids) for the L trigger
    stages_r: Tuple[Tuple[int, Tuple[int, ...]], ...] = ()
    press_l: int = 255                   
    press_r: int = 255
    key_codes: Tuple[int, ...] = ()      
    n_targets: int = 0                   # nb + 2 + len(key_codes): size of the reference-counter table


def compile_profile(data: Dict[str, Any], layout: DeviceLayout, parser: ReportParser,
                    backend: OutputBackend) -> CompiledProfile:
    """Builds a CompiledProfile. Raises ProfileError for unknown button names."""
    data = normalize_profile(data)
    warnings: List[str] = []
    nb = backend.n_buttons
    # target ids: [0..nb) — backend buttons; nb -> "LT fully", nb+1 -> "RT fully"
    special = {ANALOG_BUTTON_TARGETS[0]: nb, ANALOG_BUTTON_TARGETS[1]: nb + 1}

    if data["device"] and data["device"] != layout.id:
        raise ProfileError(tr('the profile is meant for layout {0!r}, but {1!r} is connected', data['device'], layout.id))

    bm = data["button_map"]
    for src_name in bm:
        if src_name not in parser.name_to_bit:
            raise ProfileError(tr('button_map: layout {0!r} has no input button {1!r}; available: {2}', layout.id, src_name, sorted(parser.name_to_bit)))

    target_names: List[str] = [""] * (nb + 2)
    key_ids: Dict[str, int] = {}                 # "KEY_F" -> target id (nb+2+k); one key = one target (reference counter)
    key_codes: List[int] = []
    bit_targets: List[Optional[Tuple[int, ...]]] = [None] * len(parser.src_names)
    def resolve(wanted: List[str], label: str) -> List[int]:
        """Target names -> ids (backend buttons, LT/RT, keys); unavailable ones go to warnings."""
        ids: List[int] = []
        for t in wanted:
            kc = None if t in special else key_target_code(t)
            if kc is not None and t in ALL_TARGETS:
                kc = None                                # (in case of a name clash: the gamepad target takes priority)
            if kc is not None:
                if not backend.supports_keys:
                    warnings.append(tr('{0}->{1}: this output device cannot produce keyboard/mouse', label, t))
                    continue
                if t not in key_ids:
                    key_ids[t] = nb + 2 + len(key_codes)
                    key_codes.append(kc)
                    target_names.append(t)
                ids.append(key_ids[t])
                continue
            tid = special[t] if t in special else backend.button_id(t)
            if tid is None:
                warnings.append(tr('{0}->{1}: this output device has no such button', label, t))
                continue
            ids.append(tid)
            target_names[tid] = t
        return ids

    for bit, src_name in enumerate(parser.src_names):
        if src_name is None:
            continue
        if src_name in bm:
            wanted = bm[src_name]
        elif src_name in HAT_DEFAULT_TARGETS:
            wanted = [HAT_DEFAULT_TARGETS[src_name]]
        else:
            t = layout.buttons[src_name].target
            wanted = [t] if t else []
        ids = resolve(wanted, src_name)
        bit_targets[bit] = tuple(ids) if ids else None

    stages: Dict[str, Tuple[Tuple[int, Tuple[int, ...]], ...]] = {}
    for side in ("left", "right"):
        tc = data["triggers"][side]
        if side not in layout.triggers and tc["stages"]:
            warnings.append(tr('triggers.{0}.stages: the layout does not have this trigger', side))
        lst = []
        if side in layout.triggers:
            for name in STAGE_NAMES:
                st = tc["stages"].get(name)
                if st:
                    ids2 = resolve(st["targets"], f"triggers.{side}.{name}")
                    if ids2:
                        lst.append((max(1, int(st["at"] * 255.0 + 0.5)), tuple(ids2)))
        stages[side] = tuple(lst)

    if key_codes or (data["gyro"]["enabled"] and data["gyro"]["target"] == "mouse"):
        if data["gyro"]["enabled"] and data["gyro"]["target"] == "mouse" and not backend.supports_keys:
            warnings.append(tr('gyro.target=mouse: this output device cannot produce a mouse'))
        backend.ensure_keys()                            

    idx = parser.index
    def stick(side: str) -> Optional[StickProc]:
        if side not in layout.sticks:
            return None
        fx, fy = layout.fields[layout.sticks[side][0]], layout.fields[layout.sticks[side][1]]
        return StickProc(idx[fx.name], idx[fy.name], fx, fy, data["sticks"][side])

    def trig(side: str) -> Optional[TriggerProc]:
        if side not in layout.triggers:
            return None
        f = layout.fields[layout.triggers[side]]
        return TriggerProc(idx[f.name], f, data["triggers"][side])

    gyro = None
    g = data["gyro"]
    if g["enabled"]:
        if not layout.gyro:
            raise ProfileError(tr("gyro.enabled: the layout does not describe a gyro (the 'gyro' section)"))
        act_mask = 0
        if g["activation"]:
            if g["activation"] not in parser.name_to_bit:
                raise ProfileError(tr('gyro.activation: unknown input button {0!r}', g['activation']))
            act_mask = 1 << parser.name_to_bit[g["activation"]]
        gyro = GyroProc(g, layout, parser, act_mask)

    return CompiledProfile(
        name=data["name"], data=data, parser=parser, layout=layout, n_buttons=nb,
        bit_targets=bit_targets, left=stick("left"), right=stick("right"),
        trig_l=trig("left"), trig_r=trig("right"), gyro=gyro,
        swap_sticks=data["swap_sticks"], swap_triggers=data["swap_triggers"],
        axis_ids=tuple(backend.axis_id(n) for n in ("LX", "LY", "RX", "RY", "LT", "RT")),  # type: ignore[arg-type]
        target_names=target_names, warnings=warnings,
        stages_l=stages["left"], stages_r=stages["right"],
        press_l=int(data["triggers"]["left"]["button_value"] * 255.0 + 0.5),
        press_r=int(data["triggers"]["right"]["button_value"] * 255.0 + 0.5),
        key_codes=tuple(key_codes), n_targets=nb + 2 + len(key_codes))
