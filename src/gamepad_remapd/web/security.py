"""Захист локального Web-API від атак із браузера.

Сервер слухає лише 127.0.0.1, але це НЕ робить його безпечним саме по собі:
  * CSRF / cross-site WebSocket: будь-яка відкрита у браузері сторінка може
    зробити POST/PATCH на http://127.0.0.1:8765 і змінити ваш ремапінг. Захист —
    перевірка заголовка Origin (браузер ставить його сам і сторінка не може його
    підробити).
  * DNS rebinding: зловмисний домен резолвиться в 127.0.0.1, і тоді запити
    виглядають «same-origin». Захист — перевірка заголовка Host проти білого списку.
Клієнти без браузера (curl, скрипти) Origin не надсилають — вони допускаються,
бо вже виконуються на цій машині від імені користувача.
"""
from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import urlsplit


def origin_allowed(host: Optional[str], origin: Optional[str], allowed_hosts: Iterable[str]) -> bool:
    allowed = {h.lower() for h in allowed_hosts}
    if not host or host.lower() not in allowed:
        return False                      # DNS rebinding або чужий Host
    if origin is None:
        return True                       # не-браузерний клієнт
    if origin == "null":
        return False                      # sandboxed iframe / file:// — відмовляємо
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    return netloc in allowed              # той самий хост:порт
