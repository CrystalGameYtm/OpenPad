"""OS and interpreter tuning for low latency.

Everything here is best effort: missing permissions are not fatal — the daemon
simply runs at normal priority and logs how to get the permissions.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
from ..i18n import tr

log = logging.getLogger(__name__)


def tune_interpreter(switch_interval_s: float = 0.0005) -> None:
    """CPython settings that shrink the latency tail.

        * switchinterval: the default is 5 ms — in the worst case the reader thread waits
          that long for the GIL while the asyncio thread is busy. 0.5 ms cuts this tail by
          an order of magnitude (an idle asyncio thread barely takes the GIL).
        * gc.freeze(): all objects created during startup (modules, tables) are moved out
          of the GC generations — full GC passes become almost free.
        * higher GC thresholds: the hot path only creates short-lived tuples/floats that are
          freed by reference counting; the cyclic GC is not needed there, and its pause in
          the reader thread would be extra latency.
    """
    sys.setswitchinterval(switch_interval_s)
    gc.collect()
    gc.freeze()
    gc.set_threshold(100_000, 50, 100)


def boost_current_thread(priority: int = 50) -> str:
    """Raises the priority of the CURRENT thread. Call it from the reader thread itself.

        Linux : SCHED_FIFO (real time). ``os.sched_setscheduler(0, …)`` on Linux applies to
                the calling THREAD (pid 0 = tid), not the whole process.
                Needs the RTPRIO rlimit (see packaging/linux/limits.d) or CAP_SYS_NICE.
        Windows: THREAD_PRIORITY_HIGHEST + a 1 ms timer (timeBeginPeriod).
        Returns a description of the applied mode (for logs/UI).
    """
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32                      # type: ignore[attr-defined]
            ctypes.windll.winmm.timeBeginPeriod(1)            # type: ignore[attr-defined]
            k32.SetThreadPriority(k32.GetCurrentThread(), 2)  # 2 = THREAD_PRIORITY_HIGHEST
            return "windows: THREAD_PRIORITY_HIGHEST, timer 1 ms"
        except Exception as e:  # noqa: BLE001
            log.info(tr('could not raise the thread priority: %s'), e)
            return "normal"
    try:
        prio = max(1, min(priority, os.sched_get_priority_max(os.SCHED_FIFO)))
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(prio))
        return f"SCHED_FIFO:{prio}"
    except PermissionError:
        log.info(tr("SCHED_FIFO is unavailable (no permission). Add '@input - rtprio 95' to /etc/security/limits.d (see packaging/linux/limits.d) and log in again."))
    except (AttributeError, OSError) as e:
        log.info(tr('SCHED_FIFO not applied: %s'), e)
    try:
        os.nice(-5)       
        return "nice:-5"
    except OSError:
        return "normal"
