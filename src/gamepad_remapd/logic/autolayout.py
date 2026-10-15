"""Автогенерація розкладки (DeviceLayout) із HID Report Descriptor.

Це «рівень 2» універсального мапінгу:

  рівень 1  готова розкладка за VID/PID (layouts/*.json, ~/.config/gamepad-remapd/devices/)
  рівень 2  ЦЕЙ МОДУЛЬ: розбираємо дескриптор пристрою і складаємо розкладку самі
  рівень 3  hex-sniffer (``sniff --learn``) — для vendor-режимів, де дескриптор мовчить

Що відомо ТОЧНО (береться прямо з дескриптора): де лежать осі, кнопки та hat,
у яких межах змінюються значення, довжина звіту, Report ID.

Що є ЕВРИСТИКОЮ (дескриптор цього не каже), тому розкладка позначається
``needs_confirmation`` і має бути перевірена користувачем (1 хв):
  * яка вісь — лівий/правий стік чи тригер (залежить від виробника);
  * яка кнопка — A/B/X/Y... (стандарт HID нумерує кнопки, але не називає їх).

Чого тут НЕМАЄ принципово: гіроскопа DS4-подібних пристроїв, кнопок M1–M4/C/Z у
vendor-режимі, формату rumble. Вони лежать у вендорських сторінках (0xFF00+) —
для них лишається sniffer і handshake із розкладки. Знайдені вендорські блоки
перелічено в ``_auto.notes``, щоб було з чого починати.

Усі зміщення в результаті — індекси в масиві, який повертає ``read()``, тобто
ВКЛЮЧНО з байтом Report ID (формат ``DeviceLayout``).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Optional

from .. import constants as C
from .hid_descriptor import (FLAG_RELATIVE, PAGE_BUTTON, PAGE_CONSUMER, PAGE_GENERIC_DESKTOP, PAGE_SIMULATION,
                             PAGE_VENDOR_MIN, USAGE_GAMEPAD, USAGE_HAT, USAGE_JOYSTICK,
                             USAGE_MULTI_AXIS, USAGE_RX, USAGE_RY, USAGE_RZ, USAGE_SIM_ACCELERATOR,
                             USAGE_SIM_BRAKE, USAGE_SLIDER, USAGE_X, USAGE_Y, USAGE_Z, DescriptorError,
                             HidField, ParsedDescriptor, parse_report_descriptor)
from .layout import DeviceLayout, LayoutError

log = logging.getLogger(__name__)


class AutoLayoutError(ValueError):
    """Розкладку з дескриптора скласти не можна (не геймпад або замало даних)."""


# Евристика кнопок: стандарт HID нумерує кнопки з 1, але не дає їм імен. Для більшості
# Xbox-подібних контролерів порядок такий. Це ЗДОГАДКА — вона позначена в notes.
_POSITIONAL_TARGETS = ("A", "B", "X", "Y", "LB", "RB", "BACK", "START", "LS", "RS", "GUIDE")
_AXIS_NAMES = {USAGE_X: "X", USAGE_Y: "Y", USAGE_Z: "Z", USAGE_RX: "Rx", USAGE_RY: "Ry",
               USAGE_RZ: "Rz", USAGE_SLIDER: "Slider", 0x37: "Dial", 0x38: "Wheel"}
_GAMEPAD_APPS = {(PAGE_GENERIC_DESKTOP, USAGE_GAMEPAD), (PAGE_GENERIC_DESKTOP, USAGE_JOYSTICK),
                 (PAGE_GENERIC_DESKTOP, USAGE_MULTI_AXIS)}
_FMT = {8: "8", 16: "16le", 32: "32le"}


# --------------------------------------------------------------------------
# Допоміжне
# --------------------------------------------------------------------------
def _field_def(f: HidField, base_bit: int) -> dict:
    """Опис числового поля у форматі ``DeviceLayout.fields``."""
    bit = base_bit + f.bit_offset
    if f.bit_size in _FMT and bit % 8 == 0:
        d: dict = {"offset": bit // 8, "fmt": ("s" if f.signed else "u") + _FMT[f.bit_size]}
    else:   # нестандартна ширина (12 біт) або не по межі байта -> бітове поле
        d = {"offset": bit // 8, "bit_offset": bit % 8, "bits": f.bit_size, "signed": f.signed}
    d["raw_min"], d["raw_max"] = f.lmin, f.lmax
    return d


def _pick_report(fields: List[HidField]) -> int:
    """Якщо в одній collection кілька Report ID, обираємо «найгеймпадніший»: осі й hat
    (Generic Desktop) важать вчетверо більше за кнопки. Розкладка підтримує один report_id,
    тож пристрій, що розносить осі й кнопки по різних звітах, потребує ручної розкладки."""
    score: Dict[int, int] = {}
    for f in fields:
        score[f.report_id] = score.get(f.report_id, 0) + (4 if f.usage_page == PAGE_GENERIC_DESKTOP else 1)
    return min(score, key=lambda r: (-score[r], r))


def _assign_axes(axes: Dict[int, HidField], sim: Dict[int, HidField],
                 notes: List[str]) -> Dict[str, HidField]:
    """Евристика: які осі — стіки, а які тригери. Повертає {LX, LY, RX, RY, L2, R2} -> поле."""
    roles: Dict[str, HidField] = {}
    if USAGE_X in axes and USAGE_Y in axes:
        roles["LX"], roles["LY"] = axes[USAGE_X], axes[USAGE_Y]
    z, rz, rx, ry = axes.get(USAGE_Z), axes.get(USAGE_RZ), axes.get(USAGE_RX), axes.get(USAGE_RY)
    if USAGE_SIM_BRAKE in sim or USAGE_SIM_ACCELERATOR in sim:
        # Xbox One по Bluetooth: тригери на сторінці Simulation, правий стік — Z/Rz.
        if USAGE_SIM_BRAKE in sim:
            roles["L2"] = sim[USAGE_SIM_BRAKE]
        if USAGE_SIM_ACCELERATOR in sim:
            roles["R2"] = sim[USAGE_SIM_ACCELERATOR]
        if z and rz:
            roles["RX"], roles["RY"] = z, rz
        elif rx and ry:
            roles["RX"], roles["RY"] = rx, ry
    elif z and rz and rx and ry:
        # DualShock 4 / DualSense: стіки X,Y,Z,Rz; тригери Rx,Ry.
        roles["RX"], roles["RY"], roles["L2"], roles["R2"] = z, rz, rx, ry
    elif rx and ry:
        # Класичний порядок (Xbox 360 у DInput, багато клонів): правий стік Rx,Ry.
        roles["RX"], roles["RY"] = rx, ry
        if z and rz:
            roles["L2"], roles["R2"] = z, rz
        elif z:
            notes.append("Одна вісь Z на обидва тригери (як у Xbox 360 у режимі DInput): "
                         "LT/RT не розділено — скористайтесь sniff --learn або режимом XInput.")
    elif z and rz:
        # Двостікові геймпади без Rx/Ry (Logitech F310 у DInput, Switch Pro): правий стік Z,Rz.
        roles["RX"], roles["RY"] = z, rz
    return roles


def _vendor_blocks(fields: List[HidField], base_bit: int) -> List[str]:
    """Зливає суміжні вендорські поля одного usage в блоки — це «простір пошуку» для sniffer."""
    blocks: List[List[int]] = []      # [page, usage, start_bit, size_bits]
    for f in fields:
        a = base_bit + f.bit_offset
        if blocks and blocks[-1][0] == f.usage_page and blocks[-1][1] == f.usage \
                and blocks[-1][2] + blocks[-1][3] == a:
            blocks[-1][3] += f.bit_size
        else:
            blocks.append([f.usage_page, f.usage, a, f.bit_size])
    return [f"vendor {p:04X}:{u:04X}: біти {a}..{a + n - 1} (байти {a // 8}..{(a + n - 1) // 8})"
            for p, u, a, n in blocks]


# --------------------------------------------------------------------------
# Головна функція
# --------------------------------------------------------------------------
def build_layout_dict(desc: "bytes | ParsedDescriptor", vid: int, pid: int, name: str = "",
                      interface: Optional[int] = None) -> dict:
    """Дескриптор -> словник у форматі ``DeviceLayout.from_dict``. Кидає AutoLayoutError."""
    try:
        parsed = desc if isinstance(desc, ParsedDescriptor) else parse_report_descriptor(desc)
    except DescriptorError as e:
        raise AutoLayoutError(f"дескриптор не розібрано: {e}") from None
    notes: List[str] = []

    gp_apps = {i for i, a in enumerate(parsed.applications) if a in _GAMEPAD_APPS}
    if not gp_apps:
        have = ", ".join(f"{p:04X}:{u:04X}" for p, u in parsed.applications) or "—"
        raise AutoLayoutError(f"не геймпад/джойстик (application collection: {have})")

    fields = [f for f in parsed.inputs if f.app in gp_apps and not f.flags & FLAG_RELATIVE]
    if not fields:
        raise AutoLayoutError("у геймпадній collection немає абсолютних Input-полів")
    rid = _pick_report(fields)
    other = sorted({f.report_id for f in fields} - {rid})
    if other:
        notes.append(f"Використано звіт Report ID {rid}; звіти {other} проігноровано.")
    fields = [f for f in fields if f.report_id == rid]
    base_bit = 8 if parsed.uses_report_ids else 0     # read() віддає й байт Report ID

    axes: Dict[int, HidField] = {}
    sim: Dict[int, HidField] = {}
    buttons: Dict[int, HidField] = {}
    consumer: List[HidField] = []      # 1-бітні кнопки сторінки Consumer (Home/Menu тощо)
    vendor: List[HidField] = []
    hat: Optional[HidField] = None
    for f in fields:
        if f.usage_page == PAGE_GENERIC_DESKTOP and f.usage == USAGE_HAT:
            hat = hat or f
        elif f.usage_page == PAGE_GENERIC_DESKTOP and f.usage in _AXIS_NAMES:
            axes.setdefault(f.usage, f)
        elif f.usage_page == PAGE_SIMULATION and f.usage in (USAGE_SIM_ACCELERATOR, USAGE_SIM_BRAKE):
            sim.setdefault(f.usage, f)
        elif f.usage_page == PAGE_BUTTON and f.bit_size == 1:
            buttons.setdefault(f.usage, f)
        elif f.usage_page == PAGE_CONSUMER and f.bit_size == 1:
            consumer.append(f)
        elif f.usage_page >= PAGE_VENDOR_MIN:
            vendor.append(f)

    # ---- осі ----
    out: dict = {"fields": {}}
    roles = _assign_axes(axes, sim, notes)
    used = {id(f) for f in roles.values()}
    for role in ("LX", "LY", "RX", "RY", "L2", "R2"):
        f = roles.get(role)
        if f is None:
            continue
        if f.lmax <= f.lmin or f.bit_size > 32:
            notes.append(f"Вісь {role} пропущено: некоректні межі {f.lmin}..{f.lmax}.")
            continue
        out["fields"][role] = _field_def(f, base_bit)
    for usage, f in axes.items():      # решта осей — без ролі, але видимі в UI/профілях
        if id(f) not in used and f.lmax > f.lmin and f.bit_size <= 32:
            out["fields"][f"AXIS_{_AXIS_NAMES[usage].upper()}"] = _field_def(f, base_bit)
    fld = out["fields"]
    sticks = {side: [f"{p}X", f"{p}Y"] for side, p in (("left", "L"), ("right", "R"))
              if f"{p}X" in fld and f"{p}Y" in fld}
    triggers = {side: n for side, n in (("left", "L2"), ("right", "R2")) if n in fld}
    if sticks:
        out["sticks"] = sticks
    if triggers:
        out["triggers"] = triggers
    if "RX" not in fld:
        notes.append("Правий стік не знайдено.")

    # ---- кнопки ----
    btn_defs: dict = {}
    for n in sorted(buttons):
        a = base_bit + buttons[n].bit_offset
        if n <= len(_POSITIONAL_TARGETS):
            target: Optional[str] = _POSITIONAL_TARGETS[n - 1]
        elif n - len(_POSITIONAL_TARGETS) <= len(C.EXTRA_BUTTONS):
            target = C.EXTRA_BUTTONS[n - len(_POSITIONAL_TARGETS) - 1]     # M1..M6, C, Z
        else:
            target = None
        btn_defs[f"BTN{n}"] = {"byte": a // 8, "bit": a % 8, "target": target}
    # Consumer-кнопки (напр. Flydigi кладе Home/Menu у сторінку 0x0C): що це за кнопка, невідомо,
    # тому цілі не призначаємо — вони видимі в UI й доступні для ручного ремапу.
    for f in consumer:
        a = base_bit + f.bit_offset
        btn_defs[f"CONSUMER_{f.usage:04X}"] = {"byte": a // 8, "bit": a % 8, "target": None}
    if consumer:
        notes.append(f"Знайдено {len(consumer)} кн. на сторінці Consumer без призначення "
                     "(швидше за все Home/Menu): призначте ціль вручну.")
    if btn_defs:
        out["buttons"] = btn_defs
        notes.append("Імена кнопок A/B/X/Y/LB/RB/BACK/START/LS/RS/GUIDE призначено за порядковим "
                     "номером (Xbox-розкладка) — це здогадка; перевірте та за потреби перепризначте "
                     "через button_map у профілі. Кнопки понад 11-ту віддано як M1..M6, C, Z.")

    # ---- hat ----
    if hat is not None:
        a = base_bit + hat.bit_offset
        if hat.lmax - hat.lmin + 1 != 8:
            notes.append(f"Hat має {hat.lmax - hat.lmin + 1} позицій (підтримується 8) — пропущено.")
        elif a % 8 + hat.bit_size > 8:
            notes.append("Hat перетинає межу байта — пропущено.")
        else:
            out["hat"] = {"byte": a // 8, "shift": a % 8, "mask": (1 << hat.bit_size) - 1,
                          "first": hat.lmin}

    if len(fld) < 2 and len(btn_defs) < 4:
        raise AutoLayoutError("замало елементів для геймпада "
                              f"({len(fld)} осей, {len(btn_defs)} кнопок)")

    if vendor:
        notes.append("У звіті є вендорські поля (гіроскоп, лічильники, приховані кнопки M1–M4/C/Z?) — "
                     "дескриптор не пояснює їхній зміст. Знайти їх: `gamepad-remapd sniff --learn M1,M2`.")
        notes += _vendor_blocks(vendor, base_bit)
    if parsed.output_bits:
        notes.append("Формат rumble дескриптор не описує — додайте секцію \"rumble\" у розкладку.")

    out.update({
        "_comment": "Автоматично складено з HID Report Descriptor (рівень 2). Ролі осей і імена "
                    "кнопок — евристика: перевірте та збережіть розкладку (autolayout --save).",
        "name": f"{name or f'{vid:04x}:{pid:04x}'} (auto)",
        "match": {"vid": f"{vid:04x}", "pid": f"{pid:04x}",
                  **({"interface": interface} if interface is not None else {})},
        "read_size": max(64, parsed.input_report_len(rid)),
        "_auto": {"source": "hid-descriptor", "needs_confirmation": True, "notes": notes},
    })
    if parsed.uses_report_ids:
        out["report_id"] = rid
    return out


def layout_from_descriptor(desc: "bytes | ParsedDescriptor", vid: int, pid: int, name: str = "",
                           interface: Optional[int] = None) -> DeviceLayout:
    """Те саме, але повертає готовий DeviceLayout (позначений ``generated=True``)."""
    d = build_layout_dict(desc, vid, pid, name, interface)
    lid = f"auto_{vid:04x}_{pid:04x}" + (f"_if{interface}" if interface is not None else "")
    try:
        lay = DeviceLayout.from_dict(d, lid)
    except LayoutError as e:     # баг генератора, а не користувача: не валимо демон
        raise AutoLayoutError(f"згенерована розкладка некоректна: {e}") from None
    lay.generated = True
    lay.notes = list(d["_auto"]["notes"])
    return lay


def descriptor_key(desc: bytes) -> str:
    """Короткий відбиток дескриптора (ключ кешу: той самий дескриптор -> та сама розкладка)."""
    return hashlib.sha1(desc).hexdigest()[:12]


def layout_key(info) -> str:
    """Ключ кешу авторозкладки: відбиток дескриптора (hidraw) або набір можливостей (evdev)."""
    if getattr(info, "descriptor", None):
        return descriptor_key(info.descriptor)
    ex = getattr(info, "extra", None)
    if ex:
        return hashlib.sha1(repr((ex.get("keys"), sorted(ex.get("abs", {}).items()))).encode()).hexdigest()[:12]
    return ""


def try_layout(info) -> Optional[DeviceLayout]:
    """Для Manager: DeviceInfo -> розкладка або None (без винятків, причина — в debug-лог)."""
    if getattr(info, "backend", "") == "evdev":
        if not getattr(info, "extra", None):
            return None
        from .evdev_layout import layout_from_spec
        from ..input.evdev_linux import spec_for
        return layout_from_spec(spec_for(info), info.vid, info.pid, info.name,
                                bool(info.extra.get("range_known", True)))
    if not getattr(info, "descriptor", None):
        return None
    try:
        return layout_from_descriptor(info.descriptor, info.vid, info.pid, info.name, info.interface)
    except AutoLayoutError as e:
        log.debug("%04x:%04x iface=%s: авторозкладка неможлива: %s", info.vid, info.pid,
                  info.interface, e)
        return None
