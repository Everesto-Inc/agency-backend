"""Client IP for rate limits: the edge proxy's header (Cloudflare) when configured, else N trusted XFF hops."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from starlette.requests import Request

from agency_platform.service.app import client_ip


def _request(headers: dict[str, str], peer: str = "10.42.0.7") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": (peer, 1234)})


def test_edge_header_wins_when_configured():
    req = _request({"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "10.0.1.153"})
    assert client_ip(req, 1, "CF-Connecting-IP") == "203.0.113.9"


def test_falls_back_to_xff_when_header_missing_or_not_configured():
    req = _request({"X-Forwarded-For": "198.51.100.1, 10.0.1.153"})
    assert client_ip(req, 1, "CF-Connecting-IP") == "10.0.1.153"
    assert client_ip(req, 2) == "198.51.100.1"


def test_spoofed_xff_hops_beyond_trusted_count_are_ignored():
    req = _request({"X-Forwarded-For": "6.6.6.6, 198.51.100.1, 10.0.1.153"})
    assert client_ip(req, 2) == "198.51.100.1"


def test_no_headers_uses_the_peer_address():
    assert client_ip(_request({}), 1) == "10.42.0.7"
    assert client_ip(_request({"CF-Connecting-IP": "  "}), 1, "CF-Connecting-IP") == "10.42.0.7"
