"""Профілі користувача: валідація JSON + компіляція у структури гарячого шляху.

Два етапи:
  1. `normalize_profile(dict)` — перевіряє форму та діапазони, заповнює
     значення за замовчуванням. Повертає чистий JSON-сумісний dict. Саме з
     цим dict працює JSON merge-patch (RFC 7386) гарячої зміни налаштувань.
  2. `compile_profile(...)` — зводить dict + розкладку пристрою + backend у
     `CompiledProfile`: усе вже обчислено (LUT, масштаби, таблиці бітів), а
     гарячий шлях лише читає ці структури. Компіляція ЗАВЖДИ відбувається
     поза гарячим потоком; потік підхоплює новий профіль атомарним
     присвоєнням посилання (`pipeline.cp = new_cp`).
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..constants import ALL_TARGETS, ANALOG_BUTTON_TARGETS
from ..output.base import OutputBackend
from .curves import CurveError, LUT_N, build_curve_lut, validate_curve
from .layout import (DeviceLayout, FieldDef, HAT_DEFAULT_TARGETS, ReportParser)

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ProfileError(ValueError):
    """Некоректний профіль (повідомлення містить шлях до проблемного поля)."""


# --------------------------------------------------------------------------
# 1. Нормалізація
# --------------------------------------------------------------------------
def _num(d: dict, key: str, default: float, lo: float, hi: float, path: str) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        raise ProfileError(f"{path}.{key}: очікується число")
    if not lo <= v <= hi:
        raise ProfileError(f"{path}.{key}: {v} поза діапазоном {lo}..{hi}")
    return float(v)


def _bool(d: dict, key: str, default: bool, path: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ProfileError(f"{path}.{key}: очікується true/false")
    return v


def _reject_unknown(d: dict, allowed: Tuple[str, ...], path: str) -> None:
    extra = [k for k in d if k not in allowed and not k.startswith("_")]
    if extra:
        raise ProfileError(f"{path}: невідомі ключі {extra}; дозволені {list(allowed)}")


def _curve(d: dict, path: str) -> dict:
    try:
        return validate_curve(d.get("curve"), f"{path}.curve")
    except CurveError as e:
        raise ProfileError(str(e)) from None


def _stick(d: Any, path: str) -> dict:
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise ProfileError(f"{path}: очікується об'єкт")
    _reject_unknown(d, ("inner_deadzone", "outer_deadzone", "shape", "curve",
                        "sensitivity", "invert_x", "invert_y", "swap_xy"), path)
    inner = _num(d, "inner_deadzone", 0.05, 0.0, 0.95, path)
    outer = _num(d, "outer_deadzone", 1.0, 0.05, 1.0, path)
    if outer <= inner + 0.01:
        raise ProfileError(f"{path}: outer_deadzone має бути більшим за inner_deadzone")
    shape = d.get("shape", "radial")
    if shape not in ("radial", "axial"):
        raise ProfileError(f"{path}.shape: 'radial' або 'axial'")
    return {"inner_deadzone": inner, "outer_deadzone": outer, "shape": shape,
            "curve": _curve(d, path), "sensitivity": _num(d, "sensitivity", 1.0, 0.1, 4.0, path),
            "invert_x": _bool(d, "invert_x", False, path), "invert_y": _bool(d, "invert_y", False, path),
            "swap_xy": _bool(d, "swap_xy", False, path)}


def _trigger(d: Any, path: str) -> dict:
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise ProfileError(f"{path}: очікується об'єкт")
    _reject_unknown(d, ("inner_deadzone", "outer_deadzone", "curve"), path)
    inner = _num(d, "inner_deadzone", 0.0, 0.0, 0.95, path)
    outer = _num(d, "outer_deadzone", 1.0, 0.05, 1.0, path)
    if outer <= inner + 0.01:
        raise ProfileError(f"{path}: outer_deadzone має бути більшим за inner_deadzone")
    return {"inner_deadzone": inner, "outer_deadzone": outer, "curve": _curve(d, path)}


def _gyro(d: Any, path: str) -> dict:
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise ProfileError(f"{path}: очікується об'єкт")
    _reject_unknown(d, ("enabled", "target", "activation", "deadzone_dps", "full_dps",
                        "curve", "invert_yaw", "invert_pitch", "smoothing", "bias"), path)
    target = d.get("target", "right_stick")
    if target not in ("right_stick", "left_stick"):
        raise ProfileError(f"{path}.target: 'right_stick' або 'left_stick'")
    act = d.get("activation")
    if act is not None and not isinstance(act, str):
        raise ProfileError(f"{path}.activation: ім'я вхідної кнопки або null (завжди)")
    bias = d.get("bias", [0, 0])
    if (not isinstance(bias, list) or len(bias) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bias)):
        raise ProfileError(f"{path}.bias: [yaw, pitch] у сирих одиницях датчика")
    return {"enabled": _bool(d, "enabled", False, path), "target": target, "activation": act,
            "deadzone_dps": _num(d, "deadzone_dps", 1.0, 0.0, 200.0, path),
            "full_dps": _num(d, "full_dps", 180.0, 5.0, 4000.0, path),
            "curve": _curve(d, path),
            "invert_yaw": _bool(d, "invert_yaw", False, path),
            "invert_pitch": _bool(d, "invert_pitch", False, path),
            "smoothing": _num(d, "smoothing", 0.0, 0.0, 0.95, path),
            "bias": [float(bias[0]), float(bias[1])]}


def _norm_targets(v: Any, path: str) -> List[str]:
    """Значення button_map -> список цільових кнопок ([] = кнопку вимкнено)."""
    if v == "NONE" or v == []:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ProfileError(f"{path}: ім'я цілі, список імен або [] / \"NONE\" (вимкнути)")
    for x in v:
        if x not in ALL_TARGETS:
            raise ProfileError(f"{path}: невідома ціль {x!r}; доступні {list(ALL_TARGETS)}")
    return list(dict.fromkeys(v))  # без дублікатів, зі збереженням порядку


def normalize_profile(raw: Any) -> Dict[str, Any]:
    """Перевіряє профіль і повертає повністю заповнений dict."""
    if not isinstance(raw, dict):
        raise ProfileError("профіль має бути JSON-об'єктом")
    _reject_unknown(raw, ("name", "description", "device", "button_map", "sticks", "triggers",
                          "gyro", "swap_sticks", "swap_triggers"), "profile")
    name = raw.get("name", "unnamed")
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        raise ProfileError("name: літери/цифри/._- , до 64 символів")
    bm_raw = raw.get("button_map") or {}
    if not isinstance(bm_raw, dict):
        raise ProfileError("button_map: очікується об'єкт {джерело: ціль}")
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
            raise ProfileError(f"sticks.{side}: лише left/right")
    for side in triggers:
        if side not in ("left", "right"):
            raise ProfileError(f"triggers.{side}: лише left/right")
    if out["device"] is not None and not isinstance(out["device"], str):
        raise ProfileError("device: id розкладки або null")
    return out


def merge_patch(target: Any, patch: Any) -> Any:
    """JSON Merge Patch (RFC 7386): null видаляє ключ, об'єкти зливаються рекурсивно."""
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
# 2. Компіляція: «дорога» робота один раз, поза гарячим шляхом
# --------------------------------------------------------------------------
class StickProc:
    """Обробка одного стіка: центр -> нормалізація -> deadzone -> крива -> масштаб.

    Повертає (x, y) як float у [-1, 1] (в evdev-конвенції: Y вгору = мінус).
    Кешує останній вхід: коли стік не рухається (найчастіший випадок) —
    повертає готовий результат без жодної математики.
    """
    __slots__ = ("ix", "iy", "cx", "cy", "kx", "ky", "inner", "inner2", "inv_span", "lut",
                 "radial", "sens", "inv_x", "inv_y", "swap", "_last_in", "_last_out")

    def __init__(self, ix: int, iy: int, fx: FieldDef, fy: FieldDef, cfg: dict):
        self.ix, self.iy = ix, iy
        self.cx, self.cy = fx.center, fy.center
        # kx/ky = 1/півдіапазон (знак: layout-поле `invert` для осей «вгору = плюс»)
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
                res = (0.0, 0.0)  # у мертвій зоні — рівно нуль
            else:
                m = math.sqrt(m2)
                if m > 1.0:  # «квадратні» стіки: обрізаємо до одиничного кола
                    x /= m
                    y /= m
                    m = 1.0
                t = (m - self.inner) * self.inv_span  # зовнішня deadzone -> t==1 раніше за m==1
                if t > 1.0:
                    t = 1.0
                f = t * LUT_N                          # крива: LUT + лінійна інтерполяція
                i = int(f)
                lut = self.lut
                c = (lut[i] + (lut[i + 1] - lut[i]) * (f - i)) * self.sens
                if c > 1.0:
                    c = 1.0
                s = c / m                              # масштабуємо вектор, зберігаючи напрям
                res = (x * s, y * s)
        else:  # axial: кожна вісь окремо
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
    """Тригер: сире значення -> LUT (deadzone + крива + масштаб) -> 0..255.

    LUT має по запису на КОЖНЕ можливе сире значення, тож гарячий шлях — це
    одна індексація списку.
    """
    __slots__ = ("idx", "rmin", "lut")

    def __init__(self, idx: int, f: FieldDef, cfg: dict):
        self.idx, self.rmin = idx, f.raw_min
        curve = build_curve_lut(cfg["curve"])
        inner, outer = cfg["inner_deadzone"], cfg["outer_deadzone"]
        span = float(f.raw_max - f.raw_min)
        inv = 1.0 / (outer - inner)
        lut: List[int] = []
        for v in range(f.raw_min, f.raw_max + 1):
            n = (v - f.raw_min) / span
            if n <= inner:
                lut.append(0)
                continue
            t = min(1.0, (n - inner) * inv)
            ff = t * LUT_N
            i = int(ff)
            c = curve[i] + (curve[i + 1] - curve[i]) * (ff - i)
            lut.append(int(c * 255.0 + 0.5))
        self.lut = lut

    def apply(self, vals: tuple) -> int:
        try:
            return self.lut[vals[self.idx] - self.rmin]
        except IndexError:  # значення поза заявленим діапазоном -> затиснути
            return self.lut[-1] if vals[self.idx] > self.rmin else 0


class GyroProc:
    """Гіроскоп -> додатковий вектор відхилення стіка (aim assist «гіроприціл»).

    yaw -> вісь X, pitch -> вісь Y. Швидкість обертання (°/с) після мертвої зони
    й кривої нормується на `full_dps` (швидкість, що дає повне відхилення).
    Знаки залежать від осей конкретного датчика — коригуються invert_yaw/pitch.
    """
    __slots__ = ("iyaw", "ipitch", "scale", "dz", "inv_full", "lut", "sx", "sy",
                 "bias_yaw", "bias_pitch", "alpha", "act_mask", "to_right", "_fx", "_fy")

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
        self.alpha = 1.0 - cfg["smoothing"]  # коефіцієнт EMA (1 = без згладжування)
        self.act_mask = act_mask
        self.to_right = cfg["target"] == "right_stick"
        self._fx = self._fy = 0.0

    def _axis(self, raw: float, sign: float) -> float:
        d = raw * self.scale  # °/с
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
            self._fx = self._fy = 0.0  # кнопка активації не затиснута
            return (0.0, 0.0)
        x = self._axis(vals[self.iyaw] - self.bias_yaw, self.sx)
        y = self._axis(vals[self.ipitch] - self.bias_pitch, self.sy)
        a = self.alpha
        if a < 1.0:  # експоненційне згладжування
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
    n_buttons: int                       # кількість backend-кнопок; id ≥ n_buttons — спец-цілі
    bit_targets: List[Optional[Tuple[int, ...]]]  # індекс вхідного біта -> id цілей
    left: Optional[StickProc]
    right: Optional[StickProc]
    trig_l: Optional[TriggerProc]
    trig_r: Optional[TriggerProc]
    gyro: Optional[GyroProc]
    swap_sticks: bool
    swap_triggers: bool
    axis_ids: Tuple[int, int, int, int, int, int]  # LX, LY, RX, RY, LT, RT
    target_names: List[str]              # id цілі -> ім'я (для UI)
    warnings: List[str]


def compile_profile(data: Dict[str, Any], layout: DeviceLayout, parser: ReportParser,
                    backend: OutputBackend) -> CompiledProfile:
    """Збирає CompiledProfile. Кидає ProfileError при невідомих іменах кнопок."""
    data = normalize_profile(data)
    warnings: List[str] = []
    nb = backend.n_buttons
    # id цілей: [0..nb) — кнопки backend; nb -> «LT повністю», nb+1 -> «RT повністю»
    special = {ANALOG_BUTTON_TARGETS[0]: nb, ANALOG_BUTTON_TARGETS[1]: nb + 1}

    if data["device"] and data["device"] != layout.id:
        raise ProfileError(f"профіль призначений для розкладки {data['device']!r}, "
                           f"а підключено {layout.id!r}")

    bm = data["button_map"]
    for src_name in bm:
        if src_name not in parser.name_to_bit:
            raise ProfileError(f"button_map: у розкладці {layout.id!r} немає вхідної кнопки "
                               f"{src_name!r}; є {sorted(parser.name_to_bit)}")

    target_names: List[str] = [""] * (nb + 2)
    bit_targets: List[Optional[Tuple[int, ...]]] = [None] * len(parser.src_names)
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
        ids: List[int] = []
        for t in wanted:
            tid = special[t] if t in special else backend.button_id(t)
            if tid is None:
                warnings.append(f"{src_name}->{t}: цей вихідний пристрій не має такої кнопки")
                continue
            ids.append(tid)
            target_names[tid] = t
        bit_targets[bit] = tuple(ids) if ids else None

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
            raise ProfileError("gyro.enabled: розкладка не описує гіроскоп (секція 'gyro')")
        act_mask = 0
        if g["activation"]:
            if g["activation"] not in parser.name_to_bit:
                raise ProfileError(f"gyro.activation: невідома вхідна кнопка {g['activation']!r}")
            act_mask = 1 << parser.name_to_bit[g["activation"]]
        gyro = GyroProc(g, layout, parser, act_mask)

    return CompiledProfile(
        name=data["name"], data=data, parser=parser, layout=layout, n_buttons=nb,
        bit_targets=bit_targets, left=stick("left"), right=stick("right"),
        trig_l=trig("left"), trig_r=trig("right"), gyro=gyro,
        swap_sticks=data["swap_sticks"], swap_triggers=data["swap_triggers"],
        axis_ids=tuple(backend.axis_id(n) for n in ("LX", "LY", "RX", "RY", "LT", "RT")),  # type: ignore[arg-type]
        target_names=target_names, warnings=warnings)
