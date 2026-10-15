"""Бенчмарк програмної затримки Pipeline (без заліза).

Вимірює повний ланцюжок одного звіту: фільтр -> parse (bitmask + struct) ->
XOR-diff кнопок -> deadzone+крива стіків -> збирання подій -> write() у
/dev/null (за суттю — той самий системний виклик, що й write у /dev/uinput,
без витрат ядра на доставку подій споживачам).

Що це НЕ вимірює: інтервал USB-опитування ресивера (1–8 мс — це апаратна межа,
яку софт змінити не може) та доставку події грі всередині ядра.
"""
from __future__ import annotations

import os
import random
import time
from typing import List

from ..config import BUILTIN_LAYOUTS, BUILTIN_PROFILES
from ..logic.layout import DeviceLayout, ReportParser
from ..logic.pipeline import Pipeline
from ..logic.profile import compile_profile
from ..output.uinput_linux import UinputPad, SPECS
import json


def synth_reports(layout: DeviceLayout, n: int, seed: int = 1) -> List[bytes]:
    """Правдоподібний потік: «гуляючі» стіки, випадкові кнопки/hat, повільний гіроскоп."""
    rnd = random.Random(seed)
    size = max(layout.read_size, layout.min_len)
    buf = bytearray(size)
    if layout.report_id is not None:
        buf[0] = layout.report_id
    for f in layout.fields.values():
        if f.fmt == "u8":
            buf[f.offset] = int(f.center)
    out = []
    for _ in range(n):
        for name in ("LX", "LY", "RX", "RY"):
            f = layout.fields.get(name)
            if f and f.fmt == "u8":
                buf[f.offset] = max(0, min(255, buf[f.offset] + rnd.randint(-6, 6)))
        for b in layout.buttons.values():
            if rnd.random() < 0.01:
                buf[b.byte] ^= 1 << b.bit
        out.append(bytes(buf))
    return out


def run(reports: int = 200_000, profile: str = "precision_aim", layout_id: str = "sony_ds4_usb") -> dict:
    layout = DeviceLayout.from_file(BUILTIN_LAYOUTS / f"{layout_id}.json")
    fd = os.open(os.devnull, os.O_WRONLY)
    out = UinputPad(SPECS["xbox360"], fd=fd)
    pipe = Pipeline(out)
    parser = ReportParser(layout)
    pdata = json.loads((BUILTIN_PROFILES / f"{profile}.json").read_text(encoding="utf-8"))
    pipe.cp = compile_profile(pdata, layout, parser, out)
    data = synth_reports(layout, reports)
    for r in data[:2000]:                   # прогрів: LUT у кеш процесора, байткод «гарячий»
        pipe.process(r)
    pipe.reset_stats()
    t0 = time.perf_counter()
    proc = pipe.process
    for r in data:
        proc(r)
    wall = time.perf_counter() - t0
    os.close(fd)
    st = pipe.stats()
    st["throughput_reports_per_s"] = int(reports / wall)
    st["profile"] = profile
    st["layout"] = layout_id
    return st
