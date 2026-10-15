"""Живий монітор розкладки: перевіряє, що кнопки й осі розпізнані ПРАВИЛЬНО.

    gamepad-remapd monitor /dev/hidraw8

Читає сирі звіти, розбирає їх за розкладкою (збереженою або складеною з дескриптора) і
друкує події: яку кнопку натиснуто, на яку вихідну ціль вона мапиться, та великі зміни осей.
Нічого не емулює й не змінює — безпечно запускати поруч із грою чи демоном (hidraw читають
кілька процесів одночасно).

Типовий сценарій перевірки авторозкладки: по черзі натисніть кожну кнопку — і порівняйте
ім'я в консолі з написом на геймпаді. Невідповідності виправляються в `button_map` профілю
або в розкладці (`target`).
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Dict, List, Optional, TextIO

from ..input import DeviceGone, enumerate_devices, open_device, run_handshake
from ..logic.autolayout import try_layout
from ..logic.layout import HAT_DEFAULT_TARGETS, DeviceLayout, ReportParser

AXIS_PRINT_FRACTION = 0.25      # друкуємо вісь, коли вона зсунулась на 25% діапазону від останнього виводу


def _find_layout(info, layouts: Dict[str, DeviceLayout]) -> Optional[DeviceLayout]:
    if info.backend == "evdev":
        return try_layout(info)                  # evdev: синтетичний звіт, збережені hidraw-розкладки не годяться
    cands = [l for l in layouts.values() if l.matches(info.vid, info.pid, info.interface)]
    cands.sort(key=lambda l: l.interface is None)       # конкретніші (з interface) першими
    return cands[0] if cands else try_layout(info)


def run(path: str, layouts: Dict[str, DeviceLayout], layout_id: Optional[str] = None,
        seconds: float = 0.0, backend: str = "auto", raw_dump: bool = False, *,
        enumerate_fn: Callable = enumerate_devices, open_fn: Callable = open_device,
        out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    info = next((i for i in enumerate_fn(backend) if i.path == path), None)
    if info is None:
        print(f"пристрій {path} не знайдено", file=sys.stderr)
        return 1
    layout = layouts.get(layout_id) if layout_id else _find_layout(info, layouts)
    if layout is None:
        print(f"{path}: для цього інтерфейсу немає розкладки (і він не схожий на геймпад). "
              "Спробуйте інший hidraw того ж пристрою: gamepad-remapd list", file=sys.stderr)
        return 1
    pr = ReportParser(layout)
    dev = open_fn(info, layout.read_size)
    print(f"{info.path} {info.vid:04x}:{info.pid:04x} {info.name}\nрозкладка: {layout.id}"
          f"{' (авто, потребує перевірки)' if layout.generated else ''}\n"
          "Натискайте кнопки й рухайте стіки. Ctrl-C — вихід.\n", file=out, flush=True)
    stop = threading.Event()
    timer = None
    if seconds > 0:
        timer = threading.Timer(seconds, lambda: (stop.set(), dev.wake()))
        timer.daemon = True
        timer.start()
    prev_word: Optional[int] = None
    last_axis: Dict[str, int] = {}
    n = 0
    try:
        if layout.handshake:
            run_handshake(dev, layout.handshake)
        while not stop.is_set():
            raw = dev.read()
            if raw is None:
                continue
            if raw_dump:        # діагностика: кожен звіт hex-ом; «x» = відсіяно фільтром розкладки
                bad = len(raw) < pr.min_len or (pr.rid is not None and raw[0] != pr.rid) \
                    or (pr.sync is not None and raw[pr.sync[0]] != pr.sync[1])
                print(f"{'x' if bad else ' '} {len(raw):>3}B  {raw.hex(' ')}", file=out, flush=True)
            if len(raw) < pr.min_len or (pr.rid is not None and raw[0] != pr.rid) \
                    or (pr.sync is not None and raw[pr.sync[0]] != pr.sync[1]):
                continue                                  # службовий звіт іншого типу
            n += 1
            word, vals = pr.parse(raw)
            if prev_word is None:
                prev_word = word                          # перший звіт — лише «точка відліку»
                last_axis = {name: vals[i] for name, i in pr.index.items()}
                continue
            changed = word ^ prev_word
            prev_word = word
            while changed:
                low = changed & -changed
                bit = low.bit_length() - 1
                changed ^= low
                name = pr.src_names[bit] if bit < len(pr.src_names) else None
                if name is None:
                    continue                              # біти hat-нібла та службові
                if name in layout.buttons:
                    target = layout.buttons[name].target
                else:
                    target = HAT_DEFAULT_TARGETS.get(name)
                sign = "+" if word >> bit & 1 else "-"
                print(f"{sign} {name:<16} → {target or '—'}", file=out, flush=True)
            for name, idx in pr.index.items():
                f = layout.fields[name]
                if abs(vals[idx] - last_axis[name]) >= (f.raw_max - f.raw_min) * AXIS_PRINT_FRACTION:
                    last_axis[name] = vals[idx]
                    print(f"  {name:<14} = {vals[idx]}", file=out, flush=True)
    except DeviceGone:
        print("\nпристрій відключено", file=out)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if timer:
            timer.cancel()
        dev.close()
    if n == 0:
        print("\nЗвітів не надійшло. Увімкніть геймпад і натисніть будь-яку кнопку (2.4 ГГц-геймпад "
              "засинає й тоді мовчить); переконайтесь, що це саме той hidraw-інтерфейс.", file=out)
    return 0
