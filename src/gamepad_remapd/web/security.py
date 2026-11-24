"""Protection of the local Web API against attacks from the browser.

The server listens only on 127.0.0.1, but this alone does NOT make it safe:
  * CSRF / cross-site WebSocket: any page open in the browser can send a POST/PATCH to
    http://127.0.0.1:8765 and change your remapping. The defense is checking the Origin header
    (the browser sets it itself and a page cannot forge it).
  * DNS rebinding: a malicious domain resolves to 127.0.0.1, and then the requests look
    "same-origin". The defense is checking the Host header against an allowlist.
Host and Origin only stop the browser. A request without an Origin (curl, a script) used to pass, on the
assumption that it runs on behalf of the user, but a TCP port on 127.0.0.1 is open to EVERY user of the machine. So the Web
API also needs a secret (`web.token`, mode 0600 in the user's config directory): a session cookie that the one-time login
link sets (HttpOnly, SameSite=Strict), or `Authorization: Bearer <token>` for scripts. `"web_auth": false` turns it off.
"""
from __future__ import annotations

import hmac
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


SESSION_COOKIE = "gpr_session"


def token_matches(candidate: Optional[str], token: str) -> bool:
    """Constant-time comparison; an empty or missing candidate never matches."""
    if not candidate or not token:
        return False
    return hmac.compare_digest(candidate.encode("utf-8", "surrogatepass"), token.encode("utf-8", "surrogatepass"))


def authorized(token: Optional[str], authorization: Optional[str], cookie: Optional[str]) -> bool:
    """token=None means that authentication is switched off. Otherwise the cookie or a Bearer header must carry the token."""
    if token is None:
        return True
    if token_matches(cookie, token):
        return True
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and token_matches(value.strip(), token):
            return True
    return False
