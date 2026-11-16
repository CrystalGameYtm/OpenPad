"""Automatic layout generation (DeviceLayout) from the HID Report Descriptor.

This is "level 2" of the universal mapping:

  level 1  a ready layout by VID/PID (layouts/*.json, ~/.config/gamepad-remapd/devices/)
  level 2  THIS MODULE: we parse the device descriptor and build the layout ourselves
  level 3  the hex sniffer (``sniff --learn``) — for vendor modes where the descriptor is silent

What is known EXACTLY (taken directly from the descriptor): where the axes, buttons and hat are,
the ranges their values change in, the report length, the Report ID.

What is a HEURISTIC (the descriptor does not say it), so the layout is marked
``needs_confirmation`` and must be checked by the user (about 1 minute):
  * which axis is the left/right stick or a trigger (depends on the manufacturer);
  * which button is A/B/X/Y... (HID numbers buttons but does not name them).

What is fundamentally NOT here: the gyroscope of DS4-like devices, the M1–M4/C/Z buttons in
vendor mode, the rumble format. They live in vendor pages (0xFF00+) — for them the sniffer and
the handshake from the layout remain. The vendor blocks found are listed in ``_auto.notes``
as a starting point.

All offsets in the result are indexes into the array returned by ``read()``, i.e. INCLUDING
the Report ID byte (the ``DeviceLayout`` format).
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
from ..i18n import tr

log = logging.getLogger(__name__)


class AutoLayoutError(ValueError):
    """A layout cannot be built from this descriptor (not a gamepad, or too little data)."""



# The order for Xbox-like controllers. This is a GUESS — it is marked in notes.
_POSITIONAL_TARGETS = ("A", "B", "X", "Y", "LB", "RB", "BACK", "START", "LS", "RS", "GUIDE")
_AXIS_NAMES = {USAGE_X: "X", USAGE_Y: "Y", USAGE_Z: "Z", USAGE_RX: "Rx", USAGE_RY: "Ry",
               USAGE_RZ: "Rz", USAGE_SLIDER: "Slider", 0x37: "Dial", 0x38: "Wheel"}
_GAMEPAD_APPS = {(PAGE_GENERIC_DESKTOP, USAGE_GAMEPAD), (PAGE_GENERIC_DESKTOP, USAGE_JOYSTICK),
                 (PAGE_GENERIC_DESKTOP, USAGE_MULTI_AXIS)}
_FMT = {8: "8", 16: "16le", 32: "32le"}


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
def _field_def(f: HidField, base_bit: int) -> dict:
    """Description of a numeric field in the ``DeviceLayout.fields`` format."""
    bit = base_bit + f.bit_offset
    if f.bit_size in _FMT and bit % 8 == 0:
        d: dict = {"offset": bit // 8, "fmt": ("s" if f.signed else "u") + _FMT[f.bit_size]}
    else:   
        d = {"offset": bit // 8, "bit_offset": bit % 8, "bits": f.bit_size, "signed": f.signed}
    d["raw_min"], d["raw_max"] = f.lmin, f.lmax
    return d


def _pick_report(fields: List[HidField]) -> int:
    """If one collection has several Report IDs, we pick the "most gamepad-like" one: axes and hat
    (Generic Desktop) weigh four times as much as buttons. A layout supports a single report_id,
    so a device that spreads axes and buttons across different reports needs a manual layout.
    """
    score: Dict[int, int] = {}
    for f in fields:
        score[f.report_id] = score.get(f.report_id, 0) + (4 if f.usage_page == PAGE_GENERIC_DESKTOP else 1)
    return min(score, key=lambda r: (-score[r], r))


def _assign_axes(axes: Dict[int, HidField], sim: Dict[int, HidField],
                 notes: List[str]) -> Dict[str, HidField]:
    """Heuristic: which axes are sticks and which are triggers. Returns {LX, LY, RX, RY, L2, R2} -> field."""
    roles: Dict[str, HidField] = {}
    if USAGE_X in axes and USAGE_Y in axes:
        roles["LX"], roles["LY"] = axes[USAGE_X], axes[USAGE_Y]
    z, rz, rx, ry = axes.get(USAGE_Z), axes.get(USAGE_RZ), axes.get(USAGE_RX), axes.get(USAGE_RY)
    if USAGE_SIM_BRAKE in sim or USAGE_SIM_ACCELERATOR in sim:
        # Xbox One over Bluetooth: triggers on the Simulation page, right stick — Z/Rz.
        if USAGE_SIM_BRAKE in sim:
            roles["L2"] = sim[USAGE_SIM_BRAKE]
        if USAGE_SIM_ACCELERATOR in sim:
            roles["R2"] = sim[USAGE_SIM_ACCELERATOR]
        if z and rz:
            roles["RX"], roles["RY"] = z, rz
        elif rx and ry:
            roles["RX"], roles["RY"] = rx, ry
    elif z and rz and rx and ry:
        # DualShock 4 / DualSense: sticks X,Y,Z,Rz; triggers Rx,Ry.
        roles["RX"], roles["RY"], roles["L2"], roles["R2"] = z, rz, rx, ry
    elif rx and ry:
        
        roles["RX"], roles["RY"] = rx, ry
        if z and rz:
            roles["L2"], roles["R2"] = z, rz
        elif z:
            notes.append(tr('One Z axis for both triggers (like the Xbox 360 in DInput mode): LT/RT are not separated — use sniff --learn or XInput mode.'))
    elif z and rz:
        
        roles["RX"], roles["RY"] = z, rz
    return roles


def _vendor_blocks(fields: List[HidField], base_bit: int) -> List[str]:
    """Merges adjacent vendor fields of the same usage into blocks — this is the "search space" for the sniffer."""
    blocks: List[List[int]] = []      # [page, usage, start_bit, size_bits]
    for f in fields:
        a = base_bit + f.bit_offset
        if blocks and blocks[-1][0] == f.usage_page and blocks[-1][1] == f.usage \
                and blocks[-1][2] + blocks[-1][3] == a:
            blocks[-1][3] += f.bit_size
        else:
            blocks.append([f.usage_page, f.usage, a, f.bit_size])
    return [tr('vendor {0:04X}:{1:04X}: bits {2}..{3} (bytes {4}..{5})', p, u, a, a + n - 1, a // 8, (a + n - 1) // 8)
            for p, u, a, n in blocks]


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
def build_layout_dict(desc: "bytes | ParsedDescriptor", vid: int, pid: int, name: str = "",
                      interface: Optional[int] = None) -> dict:
    """Descriptor -> dict in the ``DeviceLayout.from_dict`` format. Raises AutoLayoutError."""
    try:
        parsed = desc if isinstance(desc, ParsedDescriptor) else parse_report_descriptor(desc)
    except DescriptorError as e:
        raise AutoLayoutError(tr('descriptor could not be parsed: {0}', e)) from None
    notes: List[str] = []

    gp_apps = {i for i, a in enumerate(parsed.applications) if a in _GAMEPAD_APPS}
    if not gp_apps:
        have = ", ".join(f"{p:04X}:{u:04X}" for p, u in parsed.applications) or "—"
        raise AutoLayoutError(tr('not a gamepad/joystick (application collection: {0})', have))

    fields = [f for f in parsed.inputs if f.app in gp_apps and not f.flags & FLAG_RELATIVE]
    if not fields:
        raise AutoLayoutError(tr('the gamepad collection has no absolute Input fields'))
    rid = _pick_report(fields)
    other = sorted({f.report_id for f in fields} - {rid})
    if other:
        notes.append(tr('Report ID {0} was used; reports {1} were ignored.', rid, other))
    fields = [f for f in fields if f.report_id == rid]
    base_bit = 8 if parsed.uses_report_ids else 0     # read() also returns the Report ID byte

    axes: Dict[int, HidField] = {}
    sim: Dict[int, HidField] = {}
    buttons: Dict[int, HidField] = {}
    consumer: List[HidField] = []      # 1-bit buttons of the Consumer page (Home/Menu etc.)
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

    # ---- axes ----
    out: dict = {"fields": {}}
    roles = _assign_axes(axes, sim, notes)
    used = {id(f) for f in roles.values()}
    for role in ("LX", "LY", "RX", "RY", "L2", "R2"):
        f = roles.get(role)
        if f is None:
            continue
        if f.lmax <= f.lmin or f.bit_size > 32:
            notes.append(tr('Axis {0} skipped: invalid bounds {1}..{2}.', role, f.lmin, f.lmax))
            continue
        out["fields"][role] = _field_def(f, base_bit)
    for usage, f in axes.items():      
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
        notes.append(tr('Right stick not found.'))

    # ---- buttons ----
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
    # Consumer buttons (e.g. Flydigi puts Home/Menu on page 0x0C): what the button is, is unknown,
    
    for f in consumer:
        a = base_bit + f.bit_offset
        btn_defs[f"CONSUMER_{f.usage:04X}"] = {"byte": a // 8, "bit": a % 8, "target": None}
    if consumer:
        notes.append(tr('Found {0} button(s) on the Consumer page with no assignment (most likely Home/Menu): assign the target manually.', len(consumer)))
    if btn_defs:
        out["buttons"] = btn_defs
        notes.append(tr('The button names A/B/X/Y/LB/RB/BACK/START/LS/RS/GUIDE were assigned by ordinal (Xbox layout) — this is a guess; check it and reassign via button_map in the profile if needed. Buttons beyond the 11th are given as M1..M6, C, Z.'))

    # ---- hat ----
    if hat is not None:
        a = base_bit + hat.bit_offset
        if hat.lmax - hat.lmin + 1 != 8:
            notes.append(tr('Hat has {0} positions (8 are supported) — skipped.', hat.lmax - hat.lmin + 1))
        elif a % 8 + hat.bit_size > 8:
            notes.append(tr('Hat crosses a byte boundary — skipped.'))
        else:
            out["hat"] = {"byte": a // 8, "shift": a % 8, "mask": (1 << hat.bit_size) - 1,
                          "first": hat.lmin}

    if len(fld) < 2 and len(btn_defs) < 4:
        raise AutoLayoutError(tr('too few elements for a gamepad ({0} axes, {1} buttons)', len(fld), len(btn_defs)))

    if vendor:
        notes.append(tr('The report has vendor fields (gyroscope, counters, hidden buttons M1–M4/C/Z?) — the descriptor does not explain their meaning. To find them: `gamepad-remapd sniff --learn M1,M2`.'))
        notes += _vendor_blocks(vendor, base_bit)
    if parsed.output_bits:
        notes.append(tr('The descriptor does not describe the rumble format — add a "rumble" section to the layout.'))

    out.update({
        "_comment": tr('Composed automatically from the HID Report Descriptor (level 2). Axis roles and button names are heuristics: check them and save the layout (autolayout --save).'),
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
    """The same, but returns a ready DeviceLayout (marked ``generated=True``)."""
    d = build_layout_dict(desc, vid, pid, name, interface)
    lid = f"auto_{vid:04x}_{pid:04x}" + (f"_if{interface}" if interface is not None else "")
    try:
        lay = DeviceLayout.from_dict(d, lid)
    except LayoutError as e:     
        raise AutoLayoutError(tr('the generated layout is invalid: {0}', e)) from None
    lay.generated = True
    lay.notes = list(d["_auto"]["notes"])
    return lay


def descriptor_key(desc: bytes) -> str:
    """A short fingerprint of the descriptor (cache key: the same descriptor -> the same layout)."""
    return hashlib.sha1(desc).hexdigest()[:12]


def layout_key(info) -> str:
    """Cache key for auto-layouts: a descriptor fingerprint (hidraw) or a set of capabilities (evdev)."""
    if getattr(info, "descriptor", None):
        return descriptor_key(info.descriptor)
    ex = getattr(info, "extra", None)
    if ex:
        return hashlib.sha1(repr((ex.get("keys"), sorted(ex.get("abs", {}).items()))).encode()).hexdigest()[:12]
    return ""


def try_layout(info) -> Optional[DeviceLayout]:
    """For Manager: DeviceInfo -> layout or None (no exceptions; the reason goes to the debug log)."""
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
        log.debug(tr('%04x:%04x iface=%s: auto-layout is not possible: %s'), info.vid, info.pid,
                  info.interface, e)
        return None
