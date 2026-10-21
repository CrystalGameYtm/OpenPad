"""Бібліотека відомих handshake для автоперебору (див. tools/handshake_probe.py).

Handshake не можна «вгадати» з самого пристрою: дескриптор каже лише, які вендорські звіти існують,
але не які байти слати. Тому ми тримаємо ПЕРЕЛІК перевірених послідовностей з відкритих драйверів
(handshakes/library.json) і пробуємо їх по черзі, дивлячись, чи ожив потік звітів.

Безпека: кожен запис має `scope`.
  any      — порожній крок або лише читання feature-звітів; безпечно для будь-якого пристрою;
  vendor   — лише для пристроїв із потрібним VID (і usage page інтерфейсу, якщо задано);
  opt-in   — пакети чужого виробника: пробуються тільки при явному «широкому переборі».
Сліпого підбору байтів тут немає свідомо: вендорські команди можуть торкатись прошивки.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..util import parse_hex
from .hid_descriptor import DescriptorError, parse_report_descriptor
from .layout import DeviceLayout, HandshakeStep

BUILTIN_LIBRARY = Path(__file__).resolve().parent.parent / "handshakes"
SCOPES = ("any", "vendor", "opt-in")
_DATA_OPS = ("output", "feature_set")


class HandshakeLibError(ValueError):
    pass


@dataclass(frozen=True)
class Expect:
    """Як виглядає корисний звіт ПІСЛЯ handshake (відсікає відповіді на самі команди)."""
    report_id: Optional[int] = None
    sync: Optional[Tuple[int, int]] = None
    min_len: int = 0
    magic: Optional[Tuple[bytes, Tuple[int, ...]]] = None       # (байти, допустимі зсуви)

    @property
    def constrained(self) -> bool:
        return self.report_id is not None or self.sync is not None or self.min_len > 0 or self.magic is not None

    def matches(self, raw: bytes) -> bool:
        if len(raw) < self.min_len:
            return False
        if self.report_id is not None and (not raw or raw[0] != self.report_id):
            return False
        if self.sync is not None and (len(raw) <= self.sync[0] or raw[self.sync[0]] != self.sync[1]):
            return False
        if self.magic is not None:
            value, offsets = self.magic
            if not any(raw[o:o + len(value)] == value for o in offsets):
                return False
        return True


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    source: str
    scope: str
    steps: Tuple[HandshakeStep, ...]
    steps_json: Tuple[dict, ...]            # ті самі кроки з розгорнутими нулями, у форматі розкладки
    vids: Tuple[int, ...] = ()
    usage_pages: Tuple[int, ...] = ()
    expect: Expect = field(default_factory=Expect)
    layout: Optional[str] = None

    @property
    def is_noop(self) -> bool:
        return not self.steps


def _parse_candidate(d: dict, origin: str) -> Candidate:
    try:
        cid, scope = str(d["id"]), d.get("scope", "opt-in")
        if scope not in SCOPES:
            raise HandshakeLibError(f"{origin}:{cid}: scope має бути з {SCOPES}")
        steps: List[HandshakeStep] = []
        sj: List[dict] = []
        for i, st in enumerate(d.get("steps") or []):
            op = st.get("op")
            if op in _DATA_OPS:
                data = bytearray(parse_hex(st.get("data", "")))
                pad = int(st.get("pad_to", 0))
                if len(data) < pad:
                    data.extend(bytes(pad - len(data)))
                if not data:
                    raise HandshakeLibError(f"{origin}:{cid}: steps[{i}] без даних")
                steps.append(HandshakeStep(op, bytes(data)))
                sj.append({"op": op, "data": " ".join(f"{b:02x}" for b in data)})
            elif op == "feature_get":
                rid, ln = int(st.get("report_id", 0)), int(st.get("length", 64))
                steps.append(HandshakeStep(op, b"", rid, ln))
                sj.append({"op": op, "report_id": rid, "length": ln})
            elif op == "sleep_ms":
                ms = int(st.get("ms", 0))
                if not 0 <= ms <= 2000:
                    raise HandshakeLibError(f"{origin}:{cid}: sleep_ms 0..2000")
                steps.append(HandshakeStep(op, ms=ms))
                sj.append({"op": op, "ms": ms})
            else:
                raise HandshakeLibError(f"{origin}:{cid}: steps[{i}].op {op!r}")
        e = d.get("expect") or {}
        sync = None
        if e.get("sync"):
            sync = (int(e["sync"]["byte"]), parse_hex(str(e["sync"]["value"]))[0])
        magic = None
        if e.get("magic"):
            magic = (parse_hex(str(e["magic"]["value"])), tuple(int(o) for o in e["magic"].get("offsets", [0])))
        expect = Expect(None if e.get("report_id") is None else int(e["report_id"]), sync,
                        int(e.get("min_len", 0)), magic)
        return Candidate(
            id=cid, name=str(d.get("name", cid)), source=str(d.get("source", "")), scope=scope,
            steps=tuple(steps), steps_json=tuple(sj),
            vids=tuple(int(v, 16) for v in d.get("vids", [])),
            usage_pages=tuple(int(p, 16) for p in d.get("usage_pages", [])),
            expect=expect, layout=d.get("layout"))
    except HandshakeLibError:
        raise
    except (KeyError, ValueError, TypeError) as e:
        raise HandshakeLibError(f"{origin}: некоректний запис ({e})") from None


def load_library(*dirs: Path) -> List[Candidate]:
    """Усі *.json із каталогів (вбудований + користувацькі); id мають бути унікальні, пізніші
    каталоги перевизначають ранні. Зламаний файл -> HandshakeLibError."""
    out: Dict[str, Candidate] = {}
    for d in dirs or (BUILTIN_LIBRARY,):
        d = Path(d)
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise HandshakeLibError(f"{p.name}: {e}") from None
            for item in data.get("candidates", []):
                c = _parse_candidate(item, p.name)
                out[c.id] = c
    return list(out.values())


def _usage_pages(info) -> Optional[set]:
    desc = getattr(info, "descriptor", None)
    if not desc:
        return None
    try:
        return {p for p, _ in parse_report_descriptor(desc).applications}
    except DescriptorError:
        return None


def candidates_for(info, library: Iterable[Candidate], broad: bool = False) -> List[Candidate]:
    """Кандидати, яких дозволено пробувати на цьому інтерфейсі, у порядку «найімовірніші першими»:
    none -> vendor (збіг VID) -> any -> opt-in (лише broad)."""
    pages = _usage_pages(info)
    rank: List[Tuple[int, int, Candidate]] = []
    for n, c in enumerate(library):
        if c.scope == "any":
            r = 0 if c.is_noop else 2
        elif c.scope == "vendor":
            if info.vid not in c.vids:
                continue
            if c.usage_pages and pages is not None and not (pages & set(c.usage_pages)):
                continue
            r = 1
        else:
            if not broad:
                continue
            r = 3
        rank.append((r, n, c))
    return [c for _, _, c in sorted(rank, key=lambda t: t[:2])]


def layout_match_fraction(layout: DeviceLayout, reports: Iterable[bytes]) -> float:
    """Яка частка звітів проходить фільтр розкладки (Report ID, sync, мін. довжина)."""
    rs = list(reports)
    if not rs:
        return 0.0
    ok = 0
    for r in rs:
        if len(r) < layout.min_len:
            continue
        if layout.report_id is not None and r[0] != layout.report_id:
            continue
        if layout.sync is not None and r[layout.sync[0]] != layout.sync[1]:
            continue
        ok += 1
    return ok / len(rs)


def learned_layout_id(info) -> str:
    return f"learned_{info.vid:04x}_{info.pid:04x}" + (f"_if{info.interface}" if info.interface is not None else "")


def bind_layout(cand: Candidate, info, base: DeviceLayout) -> dict:
    """Розкладка потоку `base` для ІНШОГО пристрою тієї ж родини: власний match (VID/PID/interface)
    і знайдений handshake. Повертає dict, готовий до запису в devices/*.json."""
    if not base.source_path:
        raise HandshakeLibError(f"розкладка {base.id} не має файлу-джерела")
    d = json.loads(Path(base.source_path).read_text(encoding="utf-8"))
    d.pop("_comment", None)
    d["name"] = f"{info.name or 'gamepad'} ({cand.id}, знайдено автоматично)"
    d["match"] = {"vid": f"{info.vid:04x}", "pid": f"{info.pid:04x}"}
    if info.interface is not None:
        d["match"]["interface"] = info.interface
    d["handshake"] = [dict(s) for s in cand.steps_json]
    d["_learned"] = {"candidate": cand.id, "from_layout": base.id, "source": cand.source,
                     "note": "Розкладку потоку взято з відомої моделі тієї ж родини: перевірте кнопки "
                             "(gamepad-remapd testpage)."}
    DeviceLayout.from_dict(d, learned_layout_id(info))           # LayoutError, якщо не завантажиться
    return d
