"""Ручна перевірка вібрації без демона й гри.

    gamepad-remapd rumble-test /dev/hidraw8
    gamepad-remapd rumble-test /dev/hidraw8 --template "05 0f {strong} {weak}"
    gamepad-remapd rumble-test /dev/hidraw8 --sweep 0x05:0x00-0x20

Бере handshake і шаблон вібрації з розкладки (як демон), 1 с крутить сильний мотор, 1 с слабкий,
потім зупиняє. `--template` підміняє шаблон (плейсхолдери {strong} {weak} — u8, {strong16}/{weak16} — u16 LE),
`--sweep ID:ВІД-ДО` перебирає другий байт звіту (команду) — коли справжній формат невідомий: на
кожному значенні друкується що саме надіслано, а ви кажете, на якому затремтіло.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Dict, Optional, TextIO

from ..input import enumerate_devices, open_device, run_handshake
from ..logic.layout import DeviceLayout, RumbleSpec, _parse_rumble
from .monitor_cli import _find_layout


def _spec(template: str, pad_to: int, crc32_at: int = 0) -> RumbleSpec:
    d = {"op": "output", "template": template, "pad_to": pad_to}
    if crc32_at:
        d["crc32"] = {"seed": "a2", "at": crc32_at}            # DS4 по Bluetooth: CRC-32 від A2 + звіт[:at]
    return _parse_rumble(d)


def run(path: str, layouts: Dict[str, DeviceLayout], backend: str = "auto", template: Optional[str] = None,
        pad_to: int = 0, sweep: Optional[str] = None, hold: float = 1.0, crc32_at: int = 0, *,
        enumerate_fn: Callable = enumerate_devices, open_fn: Callable = open_device,
        sleep: Callable = time.sleep, out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    info = next((i for i in enumerate_fn(backend) if i.path == path), None)
    if info is None:
        print(f"пристрій {path} не знайдено", file=sys.stderr)
        return 1
    lay = _find_layout(info, layouts)
    specs = []                                   # (підпис, RumbleSpec)
    if sweep:
        try:
            rid, rng = sweep.split(":")
            lo, hi = (int(x, 0) for x in rng.split("-"))
            rid = int(rid, 0)
        except ValueError:
            print("--sweep: формат ID:ВІД-ДО, напр. 0x05:0x00-0x20", file=sys.stderr)
            return 1
        pad_to = pad_to or 12                    # SDL-звіти Flydigi — 12 байт (як і GET_INFO)
        for cmd in range(lo, hi + 1):
            specs.append((f"{rid:02x} {cmd:02x} {{strong}} {{weak}}", _spec(f"{rid:02x} {cmd:02x} {{strong}} {{weak}}", pad_to, crc32_at)))
    elif template:
        specs.append((template, _spec(template, pad_to, crc32_at)))
    elif lay is not None and lay.rumble is not None:
        specs.append(("з розкладки " + lay.id, lay.rumble))
    else:
        print(f"розкладка: {lay.id if lay else '—'} (bus={info.bus}, interface={info.interface}); "
              f"успадковано: {getattr(lay, 'inherited', None)}; шаблону вібрації в ній немає", file=sys.stderr)
        print("задайте --template або --sweep; якщо це DS4/GPS13B — перевірте, що встановлено оновлені "
              "logic/layout.py та layouts/sony_ds4_bt.json (grep -c inherit_from_builtin src/gamepad_remapd/logic/layout.py)",
              file=sys.stderr)
        return 2
    dev = open_fn(info, 64)
    try:
        if lay is not None and lay.handshake:
            run_handshake(dev, lay.handshake)
        print(f"розкладка: {lay.id if lay else '—'}; шаблонів: {len(specs)}", file=out)
        failed = False
        for label, spec in specs:
            if failed:
                break
            for name, s, w in (("СИЛЬНИЙ", 0xFFFF, 0), ("СЛАБКИЙ", 0, 0xFFFF)):
                data = spec.build(s, w)
                print(f"[{label}] {name}: {data.hex(' ')}", file=out, flush=True)
                try:
                    dev.write_output(data)
                except OSError as e:
                    print(f"  помилка запису: {e} — перебір зупинено. Для Errno 110 (timeout) пристрій не відповідає: "
                          "геймпад вимкнений/не з'єднаний із ресивером або ресивер завис — перевідключіть його й "
                          "спершу перевірте `monitor`.", file=out)
                    failed = True
                    break
                sleep(hold)
                dev.write_output(spec.build(0, 0))
                sleep(0.2)
    finally:
        dev.close()
    return 0
