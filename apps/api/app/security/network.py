"""
Client network identity — one implementation shared by the rate limiter
and the session writer.

Defects this replaces (both reproduced against the running app):

1. Both call sites trusted the first `X-Forwarded-For` value
   unconditionally. A client can put anything there, so rotating the
   header defeated every rate limit (5/min limit, 8 of 8 requests passed).
2. The same unvalidated string was written to `sessions.ip`
   (`$4::inet`). A proxy that appends `ip:port` (some PaaS front doors
   do), or any non-IP value, made Postgres reject the insert — breaking
   login and registration, and leaving a half-registered account behind.

Rules now: a forwarded header is honoured only for the configured number
of trusted proxies (`TRUSTED_PROXY_COUNT`, default 0 = ignore it), the
chosen entry is parsed as a real IP (an `ip:port` or `[v6]:port` form is
reduced to the address), and anything that is not an IP yields None.

Operator note — two independent trust layers exist:

* uvicorn's own `--proxy-headers` (ON by default) rewrites
  `request.client` from X-Forwarded-For BEFORE this code runs, but only
  when the TCP peer is in `--forwarded-allow-ips` (default: 127.0.0.1).
  So a client on the same host as the API — local development, or an
  nginx on the same machine — is treated as a proxy by uvicorn. This is
  uvicorn's documented behaviour, not something this module can see or
  override.
* `TRUSTED_PROXY_COUNT` is this application's own, server-independent
  rule. To make it the single authority, start uvicorn with
  `--no-proxy-headers`. Behind a same-host reverse proxy you may instead
  leave it at 0 and let uvicorn do the rewriting.

Verified against a real server from a non-loopback peer: the previous code
let 7/7 requests through a 3/min limit by rotating the header; this code
returns 429 from the 4th.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request

from ..config import get_settings


def normalize_ip(raw: str | None) -> str | None:
    """Return a canonical IP string, or None if `raw` is not an IP address."""
    if not raw:
        return None
    value = raw.strip().strip('"')
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if value.startswith("["):  # [v6]:port
        host = value[1:].partition("]")[0]
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return None
    if value.count(":") == 1:  # v4:port
        host, _, port = value.partition(":")
        if port.isdigit():
            try:
                return str(ipaddress.ip_address(host))
            except ValueError:
                return None
    return None


def client_ip(request: Request, trusted_proxy_count: int | None = None) -> str | None:
    """
    The caller's IP, or None when it cannot be established as a valid IP
    (e.g. the in-process test transport's peer is the string "testclient").
    """
    peer = normalize_ip(request.client.host if request.client else None)
    count = get_settings().trusted_proxy_count if trusted_proxy_count is None else trusted_proxy_count
    if count <= 0:
        return peer

    header = request.headers.get("x-forwarded-for", "")
    entries = [normalize_ip(part) for part in header.split(",") if part.strip()]
    if not entries:
        return peer
    # With N trusted proxies each appending its peer, the real client is
    # the Nth entry from the right; a shorter header means fewer proxies
    # than configured, so the leftmost entry is the best available.
    chosen = entries[-count] if len(entries) >= count else entries[0]
    return chosen or peer
