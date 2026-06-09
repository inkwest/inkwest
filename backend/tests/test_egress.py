"""Tests for the squid egress-log parser used by the live egress WebSocket."""

from app.api.routes.egress import parse_squid_line


def test_parse_connect_allowed():
    line = (
        "1719500000.123    142 172.18.0.3 TCP_TUNNEL/200 4096 "
        "CONNECT api.anthropic.com:443 - HIER_DIRECT/1.2.3.4 -"
    )
    event = parse_squid_line(line)
    assert event == {
        "ts": 1719500000.123,
        "decision": "TCP_TUNNEL",
        "status": "200",
        "method": "CONNECT",
        "host": "api.anthropic.com",
        "port": "443",
        "bytes": 4096,
        "client": "172.18.0.3",
    }


def test_parse_denied():
    line = (
        "1719500001.000      0 172.18.0.3 TCP_DENIED/403 3987 "
        "CONNECT evil.example.com:443 - HIER_NONE/- text/html"
    )
    event = parse_squid_line(line)
    assert event is not None
    assert event["decision"] == "TCP_DENIED"
    assert event["status"] == "403"
    assert event["host"] == "evil.example.com"
    assert event["port"] == "443"


def test_parse_malformed_returns_none():
    assert parse_squid_line("garbage") is None
    assert parse_squid_line("") is None
    assert parse_squid_line("1719500000.1 142 only four fields") is None


def test_parse_plain_http_url():
    # GET/POST log the full URL; host/port must come from the authority, not the scheme.
    line = (
        "1719500002.0 5 10.0.0.1 TCP_MISS/200 1234 "
        "GET http://archive.ubuntu.com/x/y - HIER_DIRECT/1.1.1.1 text/html"
    )
    event = parse_squid_line(line)
    assert event is not None
    assert event["host"] == "archive.ubuntu.com"
    assert event["port"] == ""
    assert event["method"] == "GET"


def test_parse_plain_http_url_with_port():
    line = (
        "1719500003.0 5 10.0.0.1 TCP_MISS/200 1234 "
        "GET http://example.com:8080/path - HIER_DIRECT/1.1.1.1 text/html"
    )
    event = parse_squid_line(line)
    assert event is not None
    assert event["host"] == "example.com"
    assert event["port"] == "8080"


def test_parse_non_numeric_fields_are_tolerated():
    # Bad timestamp and bytes shouldn't blow up — they degrade to defaults.
    line = "bad-ts 0 10.0.0.1 TCP_MISS/200 notanumber GET http://x/y - HIER_DIRECT/1.1.1.1 -"
    event = parse_squid_line(line)
    assert event is not None
    assert event["ts"] == 0.0
    assert event["bytes"] == 0
    assert event["host"] == "x"
