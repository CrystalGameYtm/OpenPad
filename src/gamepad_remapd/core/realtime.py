"""Налаштування ОС та інтерпретатора під низьку затримку.

Усе тут «best effort»: відсутність прав не фатальна — демон просто працюватиме
зі звичайним пріоритетом і напише в лог, як отримати права.
"""
from __future__ import annotations

import gc
import logging
import os
import sys

log = logging.getLogger(__name__)


def tune_interpreter(switch_interval_s: float = 0.0005) -> None:
    """Налаштування CPython, що зменшують «хвіст» затримок.

    * switchinterval: за замовчуванням 5 мс — стільки потік-читач у гіршому разі
      чекає на GIL, якщо asyncio-потік саме щось рахує. 0.5 мс скорочує цей хвіст
      на порядок (asyncio-потік у спокої майже не бере GIL).
    * gc.freeze(): усі об'єкти, створені під час старту (модулі, таблиці), виводяться
      з поколінь збирача сміття — повні проходи GC стають майже безкоштовними.
    * вищі пороги GC: гарячий шлях створює лише короткоживучі кортежі/float, що
      вивільняються лічильником посилань; циклічний GC їм не потрібен, а його
      пауза в потоці-читачі — це зайва затримка.
    """
    sys.setswitchinterval(switch_interval_s)
    gc.collect()
    gc.freeze()
    gc.set_threshold(100_000, 50, 100)


def boost_current_thread(priority: int = 50) -> str:
    """Піднімає пріоритет ПОТОЧНОГО потоку. Викликати з самого потоку-читача.

    Linux : SCHED_FIFO (реальний час). ``os.sched_setscheduler(0, …)`` у Linux
            діє на викликаючий ПОТІК (pid 0 = tid), а не на весь процес.
            Потрібен rlimit RTPRIO (див. packaging/linux/limits.d) або CAP_SYS_NICE.
    Windows: THREAD_PRIORITY_HIGHEST + таймер 1 мс (timeBeginPeriod).
    Повертає опис застосованого режиму (для логу/UI).
    """
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32                      # type: ignore[attr-defined]
            ctypes.windll.winmm.timeBeginPeriod(1)            # type: ignore[attr-defined]
            k32.SetThreadPriority(k32.GetCurrentThread(), 2)  # 2 = THREAD_PRIORITY_HIGHEST
            return "windows: THREAD_PRIORITY_HIGHEST, timer 1 ms"
        except Exception as e:  # noqa: BLE001
            log.info("не вдалося підняти пріоритет потоку: %s", e)
            return "normal"
    try:
        prio = max(1, min(priority, os.sched_get_priority_max(os.SCHED_FIFO)))
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(prio))
        return f"SCHED_FIFO:{prio}"
    except PermissionError:
        log.info("SCHED_FIFO недоступний (немає прав). Додайте у /etc/security/limits.d "
                 "'@input - rtprio 95' (див. packaging/linux/limits.d) і перелогіньтесь.")
    except (AttributeError, OSError) as e:
        log.info("SCHED_FIFO не застосовано: %s", e)
    try:
        os.nice(-5)       # хоча б вищий nice, якщо дозволено
        return "nice:-5"
    except OSError:
        return "normal"
