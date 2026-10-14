"""
`security/network.py` — client identity for rate limiting and sessions.

Regression coverage for two reproduced defects: (1) the first
X-Forwarded-For value was trusted unconditionally, so a client could
choose its own rate-limit identity; (2) the raw header string was written
to `sessions.ip` (`inet`), so a non-IP value or an `ip:port` form from a
proxy made login/registration fail. No database needed.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.security.network import client_ip, normalize_ip


def _request(peer: str | None, forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 1234) if peer is not None else None,
    }
    return Request(scope)


class TestNormalizeIp:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("203.0.113.9", "203.0.113.9"),
            (" 203.0.113.9 ", "203.0.113.9"),
            ("203.0.113.9:51234", "203.0.113.9"),  # PaaS front doors append the port
            ("2001:db8::1", "2001:db8::1"),
            ("[2001:db8::1]:443", "2001:db8::1"),
            ("::1", "::1"),
        ],
    )
    def test_valid_forms_reduce_to_the_address(self, raw, expected):
        assert normalize_ip(raw) == expected

    @pytest.mark.parametrize(
        "raw", [None, "", "   ", "unknown", "testclient", "203.0.113.999", "1.2.3.4:abc", "a, b", "'; DROP TABLE sessions;--"]
    )
    def test_anything_that_is_not_an_ip_is_none(self, raw):
        assert normalize_ip(raw) is None


class TestClientIp:
    def test_header_is_ignored_when_no_proxy_is_trusted(self):
        # The default (0). A client-supplied value must never decide identity.
        request = _request("198.51.100.7", forwarded="1.1.1.1, 2.2.2.2")
        assert client_ip(request, trusted_proxy_count=0) == "198.51.100.7"

    def test_one_trusted_proxy_uses_the_last_entry(self):
        # nginx appends the address it saw as the final entry; anything a
        # client prepended to the header sits to its left and is ignored.
        request = _request("10.0.0.5", forwarded="6.6.6.6, 203.0.113.9")
        assert client_ip(request, trusted_proxy_count=1) == "203.0.113.9"

    def test_two_trusted_proxies_use_the_second_from_the_right(self):
        request = _request("10.0.0.5", forwarded="6.6.6.6, 203.0.113.9, 10.0.0.4")
        assert client_ip(request, trusted_proxy_count=2) == "203.0.113.9"

    def test_header_shorter_than_configured_falls_back_to_leftmost(self):
        request = _request("10.0.0.5", forwarded="203.0.113.9")
        assert client_ip(request, trusted_proxy_count=3) == "203.0.113.9"

    def test_unparseable_trusted_entry_falls_back_to_the_peer(self):
        request = _request("198.51.100.7", forwarded="unknown")
        assert client_ip(request, trusted_proxy_count=1) == "198.51.100.7"

    def test_port_suffixed_entry_is_reduced_to_an_address(self):
        request = _request("10.0.0.5", forwarded="203.0.113.9:51234")
        assert client_ip(request, trusted_proxy_count=1) == "203.0.113.9"

    def test_non_ip_peer_yields_none_not_a_string_postgres_would_reject(self):
        # The in-process test transport reports the peer as "testclient".
        assert client_ip(_request("testclient"), trusted_proxy_count=0) is None

    def test_missing_peer_yields_none(self):
        assert client_ip(_request(None), trusted_proxy_count=0) is None
