"""Майстер калібрування: вчить розкладку геймпада з потоку сирих звітів («при першому ввімкненні»).

Коли для пристрою немає ні готової розкладки, ні придатного HID-дескриптора (типовий випадок —
вендорський інтерфейс ресивера 2.4 ГГц після handshake), структуру звіту можна дізнатись у
користувача: «натисніть A», «поведіть лівий стік вліво-вправо» … і спостерігати, які біти й байти
змінюються. Результат — звичайна розкладка (devices/*.json), яку далі читає той самий конвеєр.

Принципи
- Лише спостереження за звітами: нічого не пишемо в пристрій (handshake виконує викликач).
- «Шумні» байти (гіроскоп, лічильники), що мінялися в спокої, у пошуку кнопок не беруть участі.
- Кнопка = рівно ОДИН біт, що став 1 і тримався кілька звітів; кілька змін одразу = повторити.
- D-pad розпізнається і як біти (по біту на напрямок), і як hat-ніббл (8 значень по колу).
- Вісь стіка/тригера: u8 або 16-бітна (LE/BE, знакова/беззнакова) — визначається за плавністю
  значення; орієнтація Y/X (вгору/вліво = менше значення) — за тим, у який бік користувач повів першим.
- Кожен крок можна пропустити (кнопки, якої немає на цьому геймпаді).
- Годинник передається ззовні (``now``), тож ядро повністю детерміноване й тестується без сну.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .layout import DeviceLayout, LayoutError, ReportParser

HOLD_FRAMES = 3                  # скільки звітів поспіль має триматись зміна, щоб вважати її натисканням
EVAL_PERIOD_S = 0.2              # як часто перераховувати кандидатів осі
MAX_AXIS_FRAMES = 1500           # скільки останніх звітів кроку осі тримаємо для аналізу
SMOOTH_P90 = 0.35                # p90 |Δ| / розмах: плавна вісь має значення нижче
STICK_REACH = 0.35               # частка півдіапазону, яку треба пройти в кожен бік
STICK_MIN_HALF_U8 = 127.5
PERSIST_S = 0.02                 # скільки біт має триматись відмінним від спокою, щоб вважатись натисканням
BUSY_TRANSITIONS = 3             # байт, що «мерехтить» частіше за це у вікні, — шум (акселерометр, тачпад), не кнопка
TRIGGER_MIN_MOVE = {1: 60, 2: 120}   # мінімальний хід (у одиницях) для u8 / 16-бітного тригера


BASELINE_HINT = ("Візьміть геймпад у руки й трохи поворушіть його (потрясіть, поверніть), але кнопок, стіків "
                 "і тригерів НЕ чіпайте: так я дізнаюсь, які байти «шумлять» самі.")


def _popcount(v: int) -> int:
    return bin(v).count("1")


@dataclass(frozen=True)
class Item:
    kind: str                    # button | dpad | stick | trigger
    key: str                     # ім'я в розкладці (A, M1, LX, L2 ...)
    label: str
    prompt: str
    target: Optional[str] = None  # вихідна ціль кнопки
    expect_first_low: bool = False  # стік: чи має перший рух (вліво/вгору) давати МЕНШЕ значення


def default_plan() -> List[Item]:
    items: List[Item] = []
    for key, tgt, label in (("A", "A", "A"), ("B", "B", "B"), ("X", "X", "X"), ("Y", "Y", "Y"),
                            ("LB", "LB", "LB (лівий бампер)"), ("RB", "RB", "RB (правий бампер)"),
                            ("BACK", "BACK", "Back / Select"), ("START", "START", "Start / Menu"),
                            ("LS", "LS", "натискання лівого стіка (L3)"),
                            ("RS", "RS", "натискання правого стіка (R3)"),
                            ("GUIDE", "GUIDE", "Home / Guide")):
        items.append(Item("button", key, label, f"Натисніть і відпустіть: {label}", tgt))
    for key, label in (("UP", "ВГОРУ"), ("RIGHT", "ВПРАВО"), ("DOWN", "ВНИЗ"), ("LEFT", "ВЛІВО")):
        items.append(Item("dpad", key, f"D-pad {label}", f"D-pad: натисніть і відпустіть {label}", key))
    for key, label in (("M1", "M1"), ("M2", "M2"), ("M3", "M3"), ("M4", "M4"),
                       ("C", "C"), ("Z", "Z"), ("ML", "ML (бокова/задня ліва)"), ("MR", "MR (бокова/задня права)")):
        items.append(Item("button", key, f"додаткова {label}",
                          f"Додаткова кнопка {label} (задня/програмована). Немає такої — «Пропустити»", key))
    for key, label, first, low in (("LX", "ЛІВИЙ стік по горизонталі", "ВЛІВО", True),
                                   ("LY", "ЛІВИЙ стік по вертикалі", "ВГОРУ", True),
                                   ("RX", "ПРАВИЙ стік по горизонталі", "ВЛІВО", True),
                                   ("RY", "ПРАВИЙ стік по вертикалі", "ВГОРУ", True)):
        axis = "горизонталі" if key.endswith("X") else "вертикалі"
        side = "вправо" if key.endswith("X") else "вниз"
        items.append(Item("stick", key, label,
                          f"{label}: повільно до упору СПЕРШУ {first}, потім {side}, і в центр "
                          f"(рух лише по {axis}; геймпад не трясіть)", None, low))
    for key, label in (("L2", "ЛІВИЙ тригер"), ("R2", "ПРАВИЙ тригер")):
        items.append(Item("trigger", key, label, f"{label}: натисніть до упору і відпустіть", None))
    return items


@dataclass
class _Axis:
    offset: int
    fmt: str
    lo: int
    hi: int
    rest: int
    smooth: float
    first_side: int              # -1 нижче rest раніше, +1 вище, 0 — не визначено
    cur: int
    width: int


class Calibrator:
    def __init__(self, vid: int, pid: int, name: str = "", interface: Optional[int] = None, *,
                 handshake: Optional[Sequence[dict]] = None, plan: Optional[List[Item]] = None,
                 baseline_s: float = 3.0, hold_frames: int = HOLD_FRAMES):
        self.vid, self.pid, self.name, self.interface = vid, pid, name, interface
        self.handshake = [dict(s) for s in (handshake or [])]
        self.plan = plan if plan is not None else default_plan()
        self.baseline_s, self.hold = baseline_s, hold_frames
        self.phase = "baseline"            # baseline | step | release | done
        self.idx = 0
        self.message = BASELINE_HINT
        self.L = 0
        # --- baseline ---
        self._t0: Optional[float] = None
        self._last_rx: Optional[float] = None
        self._base: List[bytes] = []
        self.rest: bytes = b""
        self._rest_int = 0
        self.noisy_int = 0                 # байти, що мінялися в спокої (повністю виключаємо з кнопок)
        self.noise_span: List[int] = []
        self.sync: Optional[Tuple[int, int]] = None
        # --- результати ---
        self.buttons: Dict[str, Tuple[int, int, Optional[str]]] = {}     # name -> (byte, bit, target)
        self.hat: Optional[Tuple[int, int, int]] = None                  # (byte, shift, first)
        self.axes: Dict[str, dict] = {}
        self.sticks: Dict[str, List[str]] = {}
        self.triggers: Dict[str, str] = {}
        self.done_items: List[dict] = []
        self.warnings: List[str] = []
        self._claimed_int = 0
        self._claimed_bytes: set = set()
        self._byte0_changed = False
        self._seen = 0
        self._rx_times: List[float] = []
        # --- стан кроку ---
        self._cand = 0
        self._cand_n = 0
        self._dpad: Dict[str, dict] = {}
        self._ax_frames: List[bytes] = []
        self._ax_eval_at = 0.0
        self._ax_best: Optional[_Axis] = None
        self._release_n = 0
        self._retry = False
        self.peak_frames: Dict[str, bytes] = {}
        self._step_vals: Dict[int, set] = {}      # байт -> множина різниць зі спокоєм, що траплялись у поточному кроці
        self._dwin: deque = deque(maxlen=64)      # останні різниці «звіт XOR спокій» (з маскою шуму)

    # ===================================================================
    # ЗОВНІШНІЙ ІНТЕРФЕЙС
    # ===================================================================
    @property
    def finished(self) -> bool:
        return self.phase == "done"

    @property
    def current(self) -> Optional[Item]:
        return self.plan[self.idx] if self.phase in ("step", "release") and self.idx < len(self.plan) else None

    def status(self, now: Optional[float] = None) -> dict:
        it = self.current
        hz = 0.0
        if len(self._rx_times) >= 2 and self._rx_times[-1] > self._rx_times[0]:
            hz = round((len(self._rx_times) - 1) / (self._rx_times[-1] - self._rx_times[0]), 1)
        return {"phase": self.phase, "index": min(self.idx, len(self.plan)), "total": len(self.plan),
                "key": it.key if it else None, "kind": it.kind if it else None,
                "label": it.label if it else None,
                "prompt": (it.prompt if it else ("Готово" if self.phase == "done" else BASELINE_HINT)),
                "message": self.message, "done": list(self.done_items), "warnings": list(self.warnings),
                "reports": self._seen, "rate_hz": hz, "length": self.L, "finished": self.finished}

    def skip(self) -> None:
        """Пропустити поточний крок (немає такої кнопки/осі)."""
        it = self.current
        if it is None:
            return
        self._record(it, "пропущено", ok=False)
        self._advance()

    def finish_step(self) -> None:
        """Користувач каже «готово» для осі/тригера: беремо те, що є."""
        it = self.current
        if it is None or it.kind not in ("stick", "trigger"):
            return
        best = self._best_axis(force=True)
        if best is None:
            self.message = "Не бачу руху цієї осі. Повторіть рух або натисніть «Пропустити»."
            return
        self._accept_axis(it, best, forced=True)

    def tick(self, now: float) -> None:
        """Викликається і без звітів (перехід baseline для пристроїв, що шлють лише при зміні)."""
        if self.phase == "baseline":
            self._maybe_finish_baseline(now)

    # ===================================================================
    # ВХІД: ЗВІТИ
    # ===================================================================
    def feed(self, raw: bytes, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if not raw:
            return
        self._seen += 1
        self._rx_times.append(now)
        del self._rx_times[:-200]
        if self.phase == "baseline":
            if self._t0 is None:
                self._t0 = now
            self._last_rx = now
            if len(self._base) < 6000:
                self._base.append(bytes(raw))
            self._maybe_finish_baseline(now)
            return
        if self.phase == "done" or len(raw) != self.L:
            return                                    # службові звіти іншої довжини ігноруємо
        if raw[0] != self.rest[0]:
            self._byte0_changed = True
        d = (int.from_bytes(raw, "little") ^ self._rest_int) & self._mask()
        self._dwin.append(d)
        if d and self.phase in ("step", "release"):
            for i, v in enumerate(d.to_bytes(self.L, "little")):
                if v:
                    self._step_vals.setdefault(i, set()).add(v)       # які значення набував кожен байт за цей крок
        it = self.current
        if it is None:
            return
        if self.phase == "release":
            self._on_release(raw)
        elif it.kind in ("button", "dpad"):
            self._on_press(it, raw)
        else:
            self._on_axis(it, raw, now)

    # ===================================================================
    # BASELINE
    # ===================================================================
    def _maybe_finish_baseline(self, now: float) -> None:
        if self._t0 is None or not self._base:
            return
        elapsed = now - self._t0
        streaming = len(self._base) >= 10
        silent_for = now - (self._last_rx or now)
        if elapsed < self.baseline_s:
            return
        if not streaming and silent_for < 1.0:
            return          # пристрій шле лише при зміні: чекаємо, поки користувач відпустить (тиша ≥ 1 с)
        lens = Counter(len(r) for r in self._base)
        self.L = lens.most_common(1)[0][0]
        frames = [r for r in self._base if len(r) == self.L]
        L = self.L
        rest = bytearray(L)
        noise = [0] * L
        noisy = 0
        for i in range(L):
            col = [f[i] for f in frames]
            rest[i] = Counter(col).most_common(1)[0][0] if streaming else col[-1]
            noise[i] = max(col) - min(col)
            if len(set(col)) > 1:
                noisy |= self._noisy_bits(col) << (8 * i)
        self.rest, self._rest_int = bytes(rest), int.from_bytes(rest, "little")
        self.noisy_int, self.noise_span = noisy, noise
        # sync: однакового Report ID, але мінорна група звітів з іншим другим байтом (відповіді handshake)
        if L >= 3 and len({f[0] for f in frames}) == 1:
            c1 = Counter(f[1] for f in frames)
            top, n = c1.most_common(1)[0]
            if len(c1) > 1 and n / len(frames) >= 0.8:
                self.sync = (1, top)
        self.phase = "step"
        self.idx = 0
        self._begin_step()
        n_noisy = sum(1 for v in noise if v)
        self.message = (f"Готово: звіт {L} байт, {len(frames)} звітів у спокої"
                        f"{', шумних байтів ' + str(n_noisy) if n_noisy else ''}.")
        if streaming and len(frames) >= 2:
            span = frames and (self._rx_times[-1] - self._rx_times[0])
            if span > 0:
                pass

    @staticmethod
    def _noisy_bits(col: List[int]) -> int:
        """Маска шумних бітів байта. Зазвичай — весь байт (гіроскоп). Виняток — лічильник звітів у
        старших бітах (DS4: байт 7 = лічильник у бітах 2..7 + кнопки PS/тачпад у бітах 0..1): тоді
        виключаємо лише біти лічильника, щоб кнопки в молодших бітах можна було вивчити."""
        changed = 0
        for a, b in zip(col, col[1:]):
            changed |= a ^ b
        lo = (changed & -changed).bit_length() - 1 if changed else 0
        if lo >= 1 and changed == (0xFF >> lo) << lo:
            size = 8 - lo
            mod = 1 << size
            steps = [((b >> lo) - (a >> lo)) % mod for a, b in zip(col, col[1:])]
            if steps and sum(1 for s in steps if s in (0, 1)) >= 0.95 * len(steps):
                return changed
        return 0xFF

    # ===================================================================
    # КРОКИ
    # ===================================================================
    def _begin_step(self) -> None:
        self._step_vals = {}
        self._cand = 0
        self._cand_n = 0
        self._ax_frames = []
        self._ax_best = None
        self._ax_eval_at = 0.0
        self._release_n = 0
        self._skip_dpad_if_resolved()

    def _skip_dpad_if_resolved(self) -> None:
        pass

    def _advance(self) -> None:
        self.idx += 1
        self._retry = False
        while self.idx < len(self.plan) and self._skippable(self.plan[self.idx]):
            self.idx += 1
        if self.idx >= len(self.plan):
            self._finish()
            return
        self.phase = "step"
        self._begin_step()

    def _skippable(self, it: Item) -> bool:
        return False

    def _finish(self) -> None:
        self._resolve_dpad_if_needed()
        self.phase = "done"
        n_btn = len(self.buttons) + (4 if self.hat else 0)
        self.message = (f"Калібрування завершено: кнопок {n_btn}, осей {len(self.axes)}.")
        if not self.buttons and not self.hat and not self.axes:
            self.warnings.append("Нічого не вдалося розпізнати: розкладка буде порожньою.")

    def _record(self, it: Item, result: str, ok: bool) -> None:
        self.done_items = [d for d in self.done_items if d["key"] != it.key] + \
            [{"key": it.key, "label": it.label, "result": result, "ok": ok}]

    # ---- кнопки / D-pad ----------------------------------------------
    def _mask(self) -> int:
        return ~(self.noisy_int | self._claimed_int)

    def _hist_len(self) -> int:
        """Скільки звітів відповідає PERSIST_S при поточній частоті (не менше 3)."""
        t = self._rx_times
        hz = (len(t) - 1) / (t[-1] - t[0]) if len(t) >= 2 and t[-1] > t[0] else 125.0
        return max(3, min(30, round(hz * PERSIST_S)))

    def _persist(self) -> int:
        """Біти, що відрізняються від спокою У КОЖНОМУ з останніх H звітів і лежать у «спокійних» байтах.

        Натискання кнопки — це біт, який стабільно змінюється. Шум (акселерометр, гіроскоп, тачпад,
        що оживають, коли геймпад беруть до рук) — це байти, які мерехтять щоразу по-іншому: їх
        відсікає лічильник переходів у вікні. Через це кілька «змінених бітів» поруч із натисканням
        більше не псують розпізнавання."""
        H = self._hist_len()
        w = self._dwin
        if len(w) < H:
            return 0
        p = w[-1]
        for k in range(2, H + 1):
            p &= w[-k]
            if not p:
                return 0
        n = min(len(w), 2 * H + 1)
        frames = [w[-k] for k in range(n, 0, -1)]
        cnt = [0] * self.L
        for x, y in zip(frames, frames[1:]):
            z = x ^ y
            if z:
                for i, v in enumerate(z.to_bytes(self.L, "little")):
                    if v:
                        cnt[i] += 1
        busy = 0
        for i, c in enumerate(cnt):
            if c > BUSY_TRANSITIONS:
                busy |= 0xFF << (8 * i)
        # Кнопка за крок має лише два стани (спокій / натиснуто) -> у «чистому» байті різниця набуває не більш ніж
        # 1 значення (D-pad-hat: до 4). Байт, що набув більше — плавно дрейфує (акселерометр при нахилі від
        # натискання), навіть якщо його біти й тримаються кілька звітів поспіль.
        it = self.current
        limit = 4 if (it is not None and it.kind == "dpad") else 1
        for i, vals in self._step_vals.items():
            if len(vals) > limit:
                busy |= 0xFF << (8 * i)
                self.noisy_int |= 0xFF << (8 * i)      # дрейф сенсора доведено: далі цей байт ігноруємо в усіх кроках
        return p & ~busy & self._mask()

    def _on_press(self, it: Item, raw: bytes) -> None:
        d = self._persist()
        if d == 0:
            self._cand = 0
            self._cand_n = 0
            return
        if d == self._cand:
            self._cand_n += 1
        else:
            self._cand, self._cand_n = d, 1
        if self._cand_n < self.hold:
            return
        self._on_stable_press(it, d, raw)

    def _on_stable_press(self, it: Item, d: int, raw: bytes) -> None:
        rising = d & ~self._rest_int
        falling = d & self._rest_int
        if it.kind == "dpad":
            self._dpad[it.key] = {"rising": rising, "falling": falling, "raw": bytes(raw), "d": d}
            self._record(it, "запам'ятав", ok=True)
            self.message = f"✓ {it.label}"
            self.peak_frames[f"DPAD_{it.key}"] = bytes(raw)
            self._to_release()
            return
        if _popcount(rising) == 1 and falling == 0:
            bit = rising.bit_length() - 1
            byte, b = bit // 8, bit % 8
            self.buttons[it.key] = (byte, b, it.target)
            self._claimed_int |= 1 << bit
            self._claimed_bytes.add(byte)
            self.peak_frames[it.key] = bytes(raw)
            self._record(it, f"байт {byte}, біт {b}", ok=True)
            self.message = f"✓ {it.label}: байт {byte}, біт {b}"
            self._to_release()
            return
        n = _popcount(d)
        self.message = (f"Змінилось {n} бітів одночасно — натиснуто кілька кнопок або це не звичайна кнопка. "
                        f"Відпустіть усе й натисніть лише: {it.label}")
        self._retry = True
        self._to_release(keep_step=True)

    def _to_release(self, keep_step: bool = False) -> None:
        self.phase = "release"
        self._release_n = 0
        self._keep_step = keep_step

    def _on_release(self, raw: bytes) -> None:
        it = self.current
        if it is not None and it.kind in ("stick", "trigger"):
            return
        d = self._persist()
        self._release_n = self._release_n + 1 if d == 0 else 0
        if self._release_n < self.hold:
            return
        if getattr(self, "_keep_step", False):
            self.phase = "step"
            self._cand = self._cand_n = 0
            return
        self._advance()

    # ---- D-pad -----------------------------------------------------------
    def _resolve_dpad_if_needed(self) -> None:
        if self._dpad and self.hat is None and not any(k.startswith("PAD_") for k in self.buttons):
            self._resolve_dpad()

    def _resolve_dpad(self) -> None:
        """Розпізнає D-pad за чотирма записаними натисканнями.

        Корисне лише те, що ВІДРІЗНЯЄТЬСЯ між напрямками: байти, однаково змінені в усіх чотирьох кроках
        (акселерометр, що їде від нахилу геймпада при натисканні), нічого не кажуть про D-pad і ігноруються.
        а) по біту на напрямок: у кожного кроку є власний біт, якого немає в трьох інших;
        б) hat-ніббл: у ОДНОМУ байті значення нібла йдуть по колу up, up+2, up+4, up+6, а нейтраль
           (стан спокою) лежить поза цим діапазоном."""
        order = ("UP", "RIGHT", "DOWN", "LEFT")
        got = [self._dpad.get(k) for k in order]
        if any(g is None for g in got):
            if self._dpad:
                self.warnings.append("D-pad: пройдено не всі чотири напрямки — не розпізнано.")
            return
        # а) власний біт на напрямок
        rising = [g["rising"] for g in got]
        unique = [rising[i] & ~(rising[(i + 1) % 4] | rising[(i + 2) % 4] | rising[(i + 3) % 4]) for i in range(4)]
        if all(_popcount(u) == 1 for u in unique):
            bits = [u.bit_length() - 1 for u in unique]
            if len(set(bits)) == 4:
                for k, bit in zip(order, bits):
                    self.buttons[f"PAD_{k}"] = (bit // 8, bit % 8, k)
                    self._claimed_int |= 1 << bit
                    self._claimed_bytes.add(bit // 8)
                return
        # б) hat-ніббл
        for byte in range(1, self.L):
            if byte in {a["offset"] + k for a in self.axes.values() for k in range(a["width"])}:
                continue
            for shift in (0, 4):
                vals = [(g["raw"][byte] >> shift) & 0xF for g in got]
                up, right, down, left = vals
                neutral = (self.rest[byte] >> shift) & 0xF
                if (right, down, left) == (up + 2, up + 4, up + 6) and not up <= neutral <= up + 7:
                    self.hat = (byte, shift, up)
                    self._claimed_bytes.add(byte)
                    return
        self.warnings.append("D-pad: структуру не вдалося розпізнати (ні біти, ні hat). "
                             "Налаштуйте вручну або повторіть калібрування.")

    # ---- осі --------------------------------------------------------------
    def _on_axis(self, it: Item, raw: bytes, now: float) -> None:
        self._ax_frames.append(bytes(raw))
        del self._ax_frames[:-MAX_AXIS_FRAMES]
        if now < self._ax_eval_at:
            return
        self._ax_eval_at = now + EVAL_PERIOD_S
        best = self._best_axis()
        self._ax_best = best
        if best is None:
            return
        if self._axis_complete(it, best):
            self._accept_axis(it, best)

    def _excluded_bytes(self) -> set:
        ex = set(self._claimed_bytes)
        for a in self.axes.values():
            ex.update(range(a["offset"], a["offset"] + a["width"]))
        ex.add(0)
        return ex

    @staticmethod
    def _value(f: bytes, off: int, fmt: str) -> int:
        if fmt == "u8":
            return f[off]
        v = (f[off] | f[off + 1] << 8) if fmt.endswith("le") else (f[off] << 8 | f[off + 1])
        if fmt.startswith("s") and v >= 0x8000:
            v -= 0x10000
        return v

    @staticmethod
    def _is_16bit_pair(low: List[int], high: List[int]) -> bool:
        """Чи є два байти одним 16-бітним числом, а не двома сусідніми 8-бітними осями/шумом.

        Справжнє 16-бітне значення: молодший байт проходить майже весь діапазон, а щоразу, коли він
        «перестрибує» (255→0), старший змінюється на 1 у протилежний бік (перенос). Дві незалежні осі
        (наприклад LX, LY у DS4) такого зв'язку не мають: знаки випадкові. Без цієї перевірки
        сусідні u8-осі й шум гіроскопа сприймались як 16-бітні."""
        if max(low) - min(low) < 200:
            return False
        wraps = ok = 0
        for k in range(len(low) - 1):
            dl = low[k + 1] - low[k]
            if abs(dl) > 128:
                wraps += 1
                dh = (high[k + 1] - high[k] + 128) % 256 - 128          # різниця по модулю 256
                if dh != 0 and (dh > 0) == (dl < 0):
                    ok += 1
        return wraps >= 2 and ok >= 0.8 * wraps

    def _best_axis(self, force: bool = False) -> Optional[_Axis]:
        fr = self._ax_frames
        if len(fr) < 8:
            return None
        L = self.L
        ex = self._excluded_bytes()
        cols = [[f[i] for f in fr] for i in range(L)]
        act = [(max(c) - min(c)) - self.noise_span[i] if i not in ex else -1 for i, c in enumerate(cols)]
        it = self.current
        is_trig = it is not None and it.kind == "trigger"
        cands: List[Tuple[int, str]] = []
        for i in range(L):
            if act[i] >= 24 and i not in ex:
                cands.append((i, "u8"))
        for i in range(L - 1):
            if i in ex or i + 1 in ex:
                continue
            lo_b, hi_b = (i, i + 1)
            for a, b, suffix in ((lo_b, hi_b, "le"), (hi_b, lo_b, "be")):
                # «широкий» байт — молодший (a), старший (b) змінюється рідко
                if act[a] >= 24 and (max(cols[b]) - min(cols[b])) >= 1 and self._is_16bit_pair(cols[a], cols[b]):
                    for sg in ("u", "s"):
                        cands.append((min(i, i + 1), f"{sg}16{suffix}"))
        best: Optional[Tuple[float, int, _Axis]] = None
        seen = set()
        for off, fmt in cands:
            if (off, fmt) in seen:
                continue
            seen.add((off, fmt))
            width = 1 if fmt == "u8" else 2
            vals = [self._value(f, off, fmt) for f in fr]
            lo, hi = min(vals), max(vals)
            span = hi - lo
            if span <= 0:
                continue
            rest = self._value(self.rest, off, fmt)
            deltas = sorted(abs(vals[k + 1] - vals[k]) for k in range(len(vals) - 1))
            p90 = deltas[int(0.9 * (len(deltas) - 1))] if deltas else 0
            smooth = p90 / span
            if smooth > SMOOTH_P90 and not force:
                continue
            if not is_trig:
                tmin, tmax = (-32768, 32767) if fmt.startswith("s") else (0, 255 if width == 1 else 65535)
                mid = (tmin + tmax) / 2
                if abs(rest - mid) > 0.25 * (tmax - tmin):
                    continue
            thr_lo, thr_hi = rest - 1, rest + 1
            fs = 0
            for v in vals:
                if v <= rest - 0.2 * span and fs == 0:
                    fs = -1
                    break
                if v >= rest + 0.2 * span and fs == 0:
                    fs = 1
                    break
            a = _Axis(off, fmt, lo, hi, rest, smooth, fs, vals[-1], width)
            score = (span / ((65535 if width == 2 else 255))) if not is_trig else abs(max(hi - rest, rest - lo))
            pref = 1 if width == 2 else 0                  # 16-бітна плавна інтерпретація точніша
            key = (pref, score)
            if best is None or key > (best[1], best[0]):
                best = (score, pref, a)
        return best[2] if best else None

    def _settled(self, a: _Axis) -> bool:
        """Вісь «заспокоїлась»: останні ~12 звітів майже не змінюються. Без цього молодший байт
        16-бітного значення, що проходить через «спокій» посеред руху, схожий на завершену 8-бітну вісь."""
        tail = [self._value(f, a.offset, a.fmt) for f in self._ax_frames[-12:]]
        if len(tail) < 12:
            return False
        return max(tail) - min(tail) <= max(3, 0.03 * (a.hi - a.lo))

    def _axis_complete(self, it: Item, a: _Axis) -> bool:
        if not self._settled(a):
            return False
        if it.kind == "trigger":
            far = max(a.hi - a.rest, a.rest - a.lo)
            need = TRIGGER_MIN_MOVE[a.width]
            if far < need:
                return False
            return abs(a.cur - a.rest) <= 0.15 * far
        half = 32768.0 if a.width == 2 else STICK_MIN_HALF_U8
        reach = STICK_REACH * half
        if not (a.lo <= a.rest - reach and a.hi >= a.rest + reach):
            return False
        return abs(a.cur - a.rest) <= 0.2 * half

    def _accept_axis(self, it: Item, a: _Axis, forced: bool = False) -> None:
        d: dict = {"offset": a.offset, "fmt": a.fmt, "width": a.width}
        if it.kind == "trigger":
            rest, far_hi = a.rest, a.hi
            inverted = (a.rest - a.lo) > (a.hi - a.rest)
            ext = a.lo if inverted else a.hi
            m = 255
            for cap in (255, 1023, 4095, 65535):
                if max(ext, a.rest) <= cap:
                    m = cap
                    break
            d.update(raw_min=0, raw_max=m)
            if inverted:
                d["invert"] = True
            self.triggers["left" if it.key == "L2" else "right"] = it.key
        else:
            d["center"] = a.rest
            if it.expect_first_low and a.first_side == 1:
                d["invert"] = True
                self.warnings.append(f"{it.key}: напрямок інвертовано за вашим першим рухом.")
            side = "left" if it.key.startswith("L") else "right"
            self.sticks.setdefault(side, [None, None])[0 if it.key.endswith("X") else 1] = it.key
        self.axes[it.key] = d
        self.peak_frames[it.key] = self._ax_frames[-1] if self._ax_frames else b""
        self._record(it, f"байт {a.offset}, {a.fmt}" + (" (за кнопкою «Готово»)" if forced else ""), ok=True)
        self.message = f"✓ {it.label}: байт {a.offset}, формат {a.fmt}"
        self._advance()

    # ===================================================================
    # РЕЗУЛЬТАТ
    # ===================================================================
    def build(self) -> dict:
        """Словник розкладки (формат devices/*.json). LayoutError, якщо результат не завантажується."""
        if self.phase != "done":
            self._resolve_dpad_if_needed()
        d: dict = {
            "name": f"{self.name or 'gamepad'} (калібрування)",
            "match": {"vid": f"{self.vid:04x}", "pid": f"{self.pid:04x}"},
            "read_size": max(64, self.L),
            "priority": 10,
        }
        if self.interface is not None:
            d["match"]["interface"] = self.interface
        if self.L and not self._byte0_changed and self.rest:
            used = {b for b, _, _ in self.buttons.values()} | set().union(
                *[set(range(a["offset"], a["offset"] + a["width"])) for a in self.axes.values()] or [set()])
            if self.hat:
                used.add(self.hat[0])
            if 0 not in used:
                d["report_id"] = self.rest[0]
                if self.sync:
                    d["sync"] = {"byte": self.sync[0], "value": f"{self.sync[1]:02x}"}
        if self.handshake:
            d["handshake"] = self.handshake
        d["buttons"] = {k: {"byte": b, "bit": bit, "target": t} for k, (b, bit, t) in self.buttons.items()}
        if self.hat:
            byte, shift, first = self.hat
            d["hat"] = {"byte": byte, "shift": shift, "mask": 15, "first": first}
        fields: Dict[str, dict] = {}
        for k, a in self.axes.items():
            f: dict = {"offset": a["offset"]}
            if a["fmt"] != "u8" or True:
                f["fmt"] = a["fmt"]
            for opt in ("center", "raw_min", "raw_max", "invert"):
                if opt in a:
                    f[opt] = a[opt]
            fields[k] = f
        if fields:
            d["fields"] = fields
        sticks = {s: v for s, v in self.sticks.items() if v[0] in fields and v[1] in fields}
        if sticks:
            d["sticks"] = sticks
        trig = {s: k for s, k in self.triggers.items() if k in fields}
        if trig:
            d["triggers"] = trig
        d["_learned"] = {"method": "calibration", "notes": list(self.warnings),
                         "note": "Розкладку складено майстром калібрування: перевірте кнопки (gamepad-remapd testpage)."}
        return d

    def layout(self, layout_id: Optional[str] = None) -> DeviceLayout:
        lid = layout_id or (f"calibrated_{self.vid:04x}_{self.pid:04x}"
                            + (f"_if{self.interface}" if self.interface is not None else ""))
        return DeviceLayout.from_dict(self.build(), lid)

    def verify(self) -> List[str]:
        """Самоперевірка на збережених кадрах: у кадрі кнопки X має бути натиснута саме X."""
        issues: List[str] = []
        try:
            lay = self.layout()
        except LayoutError as e:
            return [f"розкладка не завантажується: {e}"]
        pr = ReportParser(lay)
        rest_word, _ = pr.parse(self.rest) if self.rest and len(self.rest) >= pr.min_len else (0, ())
        named = sum(1 << i for i in pr.name_to_bit.values())     # безіменні біти (hat-ніббл, лічильник) не рахуємо
        if rest_word & named:
            issues.append("у стані спокою розкладка бачить натиснуті кнопки")
        for key, frame in self.peak_frames.items():
            if key in lay.buttons and len(frame) >= pr.min_len:
                word, _ = pr.parse(frame)
                bit = pr.name_to_bit.get(key)
                if bit is not None and not (word >> bit) & 1:
                    issues.append(f"кнопка {key}: у кадрі натискання не розпізнається")
        return issues
