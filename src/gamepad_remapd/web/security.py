"""Protection of the local Web API against attacks from the browser.

The server listens only on 127.0.0.1, but this alone does NOT make it safe:
  * CSRF / cross-site WebSocket: any page open in the browser can send a POST/PATCH to
    http://127.0.0.1:8765 and change your remapping. The defense is checking the Origin header
    (the browser sets it itself and a page cannot forge it).
  * DNS rebinding: a malicious domain resolves to 127.0.0.1, and then the requests look
    "same-origin". The defense is checking the Host header against an allowlist.
Clients without a browser (curl, scripts) send no Origin — they are allowed, because they
already run on this machine on behalf of the user.
"""
from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import urlsplit


def origin_allowed(host: Optional[str], origin: Optional[str], allowed_hosts: Iterable[str]) -> bool:
    allowed = {h.lower() for h in allowed_hosts}
    if not host or host.lower() not in allowed:
        return False                      # DNS rebinding or a foreign Host
    if origin is None:
        return True                       
    if origin == "null":
        return False                      # sandboxed iframe / file:// — we refuse
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    return netloc in allowed              
