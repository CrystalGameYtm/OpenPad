"""ГАРЯЧИЙ ШЛЯХ: сирий HID-звіт -> парсинг -> мапінг -> віртуальний геймпад.

Один виклик `Pipeline.process(raw)` виконує весь ланцюжок синхронно у потоці
читача. Жодних asyncio-переходів, черг, копіювань між потоками чи алокацій
великих об'єктів: затримка програмної обробки — це час самого виклику
(вимірюється вбудованою гістограмою, див. `stats()`).

Мапінг кнопок працює на бітових масках та XOR-diff:
    changed = src ^ src_prev
Обробляються ЛИШЕ біти, що змінились (зазвичай 0–2 за звіт), тому вартість не
залежить від кількості кнопок. Для кожної цілі ведеться лічильник посилань:
якщо дві фізичні кнопки відображені на одну віртуальну, вона відпускається
лише коли відпущено обидві. Кількість одночасно утримуваних кнопок нічим не
обмежена (немає 6KRO/NKRO-масивів — лише бітова маска довільної довжини).
"""
from __future__ import annotations

from math import sqrt  # noqa: F401  (залишено для профайлерів; sqrt використовується у StickProc)
from time import perf_counter_ns
from typing import List, Optional

from ..output.base import OutputBackend
from .curves import LUT_N  # noqa: F401
from .profile import CompiledProfile

HIST_BUCKETS = 2048  # 1 мкс на кошик; усе, що довше 2 мс, потрапляє в останній


class Pipeline:
    __slots__ = ("out", "cp", "_seen", "ref", "src_prev", "last", "full_l", "full_r",
                 "count", "hist", "max_ns", "total_ns", "sniffer", "last_raw", "dropped")

    def __init__(self, out: OutputBackend):
        self.out = out
        self.cp: Optional[CompiledProfile] = None   # підміняється з control-потоку
        self._seen: Optional[CompiledProfile] = None
        self.ref: List[int] = []                    # лічильники посилань цілей
        self.src_prev = 0                           # попередня маска вхідних кнопок
        self.last = [0, 0, 0, 0, 0, 0]              # останні віддані LX LY RX RY LT RT
        self.full_l = self.full_r = 0               # «цифровий» LT/RT утримується?
        self.count = 0
        self.hist = [0] * HIST_BUCKETS
        self.max_ns = 0
        self.total_ns = 0
        self.sniffer = None                         # HexSniffer | None
        self.last_raw = b""
        self.dropped = 0                            # звіти, відкинуті фільтром

    # ---- керування (викликається з потоку читача) ----------------------
    def _rebind(self, cp: CompiledProfile) -> None:
        """Перехід на новий профіль: скидаємо стан і «передаємо» утримувані
        кнопки заново, щоб нова мапа діяла без відпускання/натискання."""
        self.out.release_all()
        self.ref = [0] * (cp.n_targets or cp.n_buttons + 2)
        self.src_prev = 0       # наступний звіт побачить усі утримувані кнопки як «нові»
        self.last = [0, 0, 0, 0, 0, 0]
        self.full_l = self.full_r = 0
        self._seen = cp

    def reset_stats(self) -> None:
        self.count = 0
        self.hist = [0] * HIST_BUCKETS
        self.max_ns = 0
        self.total_ns = 0
        self.dropped = 0

    def stats(self) -> dict:
        """Статистика часу обробки одного звіту (мкс), з 1-мкс гістограми."""
        n = self.count
        if not n:
            return {"count": 0, "dropped": self.dropped}

        def pct(p: float) -> int:
            target, acc = n * p, 0
            for i, c in enumerate(self.hist):
                acc += c
                if acc >= target:
                    return i
            return HIST_BUCKETS - 1
        return {"count": n, "dropped": self.dropped,
                "mean_us": round(self.total_ns / n / 1000, 2),
                "p50_us": pct(0.50), "p99_us": pct(0.99), "p999_us": pct(0.999),
                "max_us": round(self.max_ns / 1000, 1)}

    # ---- ГАРЯЧИЙ ШЛЯХ ------------------------------------------------------
    def process(self, raw: bytes) -> None:
        t0 = perf_counter_ns()
        sn = self.sniffer
        if sn is not None:          # режим hex-sniffing: віддаємо сирі байти інспектору
            sn.feed(raw)
        cp = self.cp
        if cp is None:              # профіль ще не скомпільовано (пристрій щойно підключили)
            return
        if cp is not self._seen:    # control-потік підмінив профіль -> гаряча заміна
            self._rebind(cp)
        parser = cp.parser
        # Фільтр: коротші звіти / інший Report ID (службові звіти ресивера) ігноруємо.
        if len(raw) < parser.min_len or (parser.rid is not None and raw[0] != parser.rid) \
                or (parser.sync is not None and raw[parser.sync[0]] != parser.sync[1]):
            self.dropped += 1
            return
        self.last_raw = raw

        # 1) ПАРСИНГ: два виклики C-коду -> маска кнопок + кортеж числових полів.
        src, vals = parser.parse(raw)
        out = self.out
        ref = self.ref

        # 2) КНОПКИ: XOR-diff із попереднім станом; чіпаємо лише змінені біти.
        changed = src ^ self.src_prev
        if changed:
            self.src_prev = src
            table = cp.bit_targets
            nb = cp.n_buttons
            while changed:
                low = changed & -changed            # наймолодший змінений біт
                changed ^= low
                targets = table[low.bit_length() - 1]
                if targets is None:
                    continue
                if src & low:                       # натиснуто
                    for t in targets:
                        r = ref[t]
                        ref[t] = r + 1
                        if r == 0:                  # перша «ручка» на цій цілі
                            if t < nb:
                                out.set_button(t, True)
                            elif t == nb:
                                self.full_l = 255
                            elif t == nb + 1:
                                self.full_r = 255
                            else:                       # клавіша клавіатури / кнопка миші
                                out.set_key(cp.key_codes[t - nb - 2], True)
                else:                               # відпущено
                    for t in targets:
                        r = ref[t] - 1
                        ref[t] = r
                        if r == 0:                  # остання «ручка» відпущена
                            if t < nb:
                                out.set_button(t, False)
                            elif t == nb:
                                self.full_l = 0
                            elif t == nb + 1:
                                self.full_r = 0
                            else:
                                out.set_key(cp.key_codes[t - nb - 2], False)

        # 3) ОСІ: стіки (deadzone+крива), гіроскоп, тригери.
        left, right = cp.left, cp.right
        lx, ly = left.apply(vals) if left is not None else (0.0, 0.0)
        rx, ry = right.apply(vals) if right is not None else (0.0, 0.0)
        gyro = cp.gyro
        if gyro is not None:
            gx, gy = gyro.apply(vals, src)
            if gx or gy:
                if gyro.to_right:
                    rx += gx
                    ry += gy
                else:
                    lx += gx
                    ly += gy
        if cp.swap_sticks:
            lx, ly, rx, ry = rx, ry, lx, ly
        tl, tr = cp.trig_l, cp.trig_r
        lt = tl.apply(vals) if tl is not None else 0
        rt = tr.apply(vals) if tr is not None else 0
        if cp.swap_triggers:
            lt, rt = rt, lt
        if self.full_l and lt < 255:                # цифрова ціль «LT» -> повний тригер
            lt = 255
        if self.full_r and rt < 255:
            rt = 255

        # float [-1,1] -> int16 з затисканням (сума гіро + стік може вийти за межі)
        ax = (int(_clamp(lx) * 32767.0), int(_clamp(ly) * 32767.0),
              int(_clamp(rx) * 32767.0), int(_clamp(ry) * 32767.0), lt, rt)
        last = self.last
        ids = cp.axis_ids
        for k in range(6):                          # віддаємо лише змінені осі
            v = ax[k]
            if v != last[k]:
                last[k] = v
                out.set_axis(ids[k], v)

        # 4) ВИВІД: один системний виклик на звіт (усі зміни + SYN_REPORT).
        out.flush()

        # 5) СТАТИСТИКА затримки програмної обробки.
        dt = perf_counter_ns() - t0
        self.count += 1
        self.total_ns += dt
        if dt > self.max_ns:
            self.max_ns = dt
        b = dt // 1000
        self.hist[b if b < HIST_BUCKETS else HIST_BUCKETS - 1] += 1


def _clamp(v: float) -> float:
    return -1.0 if v < -1.0 else 1.0 if v > 1.0 else v
