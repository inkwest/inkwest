"""Tests for the squid egress-log parser used by the live egress WebSocket,
and a contract test that locks the sandbox egress allowlist policy.
"""

import re
from pathlib import Path

import pytest

from app.api.routes.egress import parse_squid_line

# The reference allowlist the backend bind-mounts into every per-conversation proxy.
# Both engines (claude-cli + inkwest-agent) egress only through this — the agent's
# boto3 Converse calls inherit HTTPS_PROXY and the --internal network forbids any other
# route, so this file IS the model-egress boundary.
_SQUID_CONF = Path(__file__).resolve().parents[2] / "sandbox" / "squid.conf"


def _require_squid_conf() -> None:
    """Skip (don't error) when the ACL file isn't in this run context — e.g. a container
    that mounts only ``backend/`` at the repo root, so the sibling ``sandbox/`` is absent.
    The parser tests below don't need it; the allowlist-contract tests do and run in CI /
    a full checkout where ``sandbox/squid.conf`` is present."""
    if not _SQUID_CONF.exists():
        pytest.skip(f"{_SQUID_CONF} not present in this run context")


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


# --------------------------------------------------------------------------- #
# Egress allowlist contract: the sandbox proxy must let the model
# endpoints through and keep the exfil channels closed. This emulates squid's
# http_access evaluation against the checked-in squid.conf so a future edit that
# silently opens egress (or breaks Bedrock) fails the build.
# --------------------------------------------------------------------------- #
def _parse_squid_acls() -> tuple[list[str], dict[str, list[re.Pattern]]]:
    """Extract the dstdomain tokens of `allowed_domains` + the dstdom_regex ACLs."""
    _require_squid_conf()
    domains: list[str] = []
    regexes: dict[str, list[re.Pattern]] = {}
    for raw in _SQUID_CONF.read_text().splitlines():
        line = raw.strip()
        if not line.startswith("acl "):
            continue
        parts = line.split()
        _, name, kind, *rest = parts
        if kind == "dstdomain" and name == "allowed_domains":
            domains.extend(rest)
        elif kind == "dstdom_regex":
            regexes.setdefault(name, []).append(re.compile(rest[0]))
    return domains, regexes


def _match_dstdomain(host: str, tokens: list[str]) -> bool:
    # squid: a leading-dot token matches the domain itself and any subdomain; a bare
    # token matches exactly.
    for t in tokens:
        if t.startswith("."):
            if host == t[1:] or host.endswith(t):
                return True
        elif host == t:
            return True
    return False


def _decide(host: str) -> str:
    """Mirror the squid.conf http_access block (first match wins)."""
    domains, regexes = _parse_squid_acls()
    if any(p.match(host) for p in regexes.get("aws_s3_re", [])):
        return "deny"  # S3 denied before any allow
    if _match_dstdomain(host, domains):
        return "allow"
    if any(p.match(host) for p in regexes.get("allowed_domains_re", [])):
        return "allow"
    if any(p.match(host) for p in regexes.get("aws_control_plane_re", [])):
        return "allow"
    return "deny"  # default deny all (fail closed)


def test_squid_http_access_order_is_what_the_emulator_assumes():
    # The emulator above hard-codes the rule order; assert the conf still matches it so a
    # reorder can't make these tests lie.
    _require_squid_conf()
    text = _SQUID_CONF.read_text()
    order = [
        "http_access deny localnet aws_s3_re",
        "http_access allow localnet allowed_domains",
        "http_access allow localnet allowed_domains_re",
        "http_access allow localnet aws_control_plane_re",
        "http_access deny all",
    ]
    positions = [text.index(rule) for rule in order]  # raises if any rule is gone
    assert positions == sorted(positions), "squid http_access rules reordered"


@pytest.mark.parametrize(
    "host",
    [
        "bedrock-runtime.eu-west-1.amazonaws.com",  # Converse (agent), eu-west-1
        "bedrock-runtime.us-east-1.amazonaws.com",  # cross-region inference profile
        "bedrock.eu-west-1.amazonaws.com",
        "bedrock-agent-runtime.eu-west-1.amazonaws.com",
        "api.anthropic.com",  # claude-cli / Anthropic-direct
        "sts.amazonaws.com",  # global STS (creds)
        "iam.amazonaws.com",  # global IAM
        "dynamodb.eu-west-1.amazonaws.com",  # single-label control-plane service, in-region
        "ec2.us-east-1.amazonaws.com",  # global/us-east-1 control plane
        "abc123.gr7.eu-west-1.eks.amazonaws.com",  # EKS API server — region-agnostic, core use case
        "cluster.eu-central-1.eks.amazonaws.com",  # EKS in another region still reachable
        # P1.7: the Coralogix MCP server — both engines reach it through this
        # proxy (claude-cli's in-CLI MCP + the inkwest-agent's in-sandbox MCP client). The
        # endpoint is api.<region>.coralogix.com/mgmt/api/v1/mcp; covered by .coralogix.com.
        "api.eu2.coralogix.com",
        "api.coralogix.com",
        # opencode engine: provider/model catalog fetched from models.dev every run.
        "models.dev",
    ],
)
def test_model_and_aws_control_endpoints_allowed(host):
    assert _decide(host) == "allow"


@pytest.mark.parametrize(
    "host",
    [
        "s3.eu-west-1.amazonaws.com",  # path-style S3 — denied before the broad allow
        "s3.amazonaws.com",
        "s3-accesspoint.eu-west-1.amazonaws.com",
        # Virtual-hosted-style S3 (the default for the AWS CLI/SDKs) — now denied too, in
        # every region and dualstack form (security fix; was the exfil bypass).
        "my-bucket.s3.amazonaws.com",
        "my-bucket.s3.eu-west-1.amazonaws.com",
        "evil.s3.us-east-1.amazonaws.com",
        "my.nested.bucket.s3.dualstack.us-east-1.amazonaws.com",
        "evil.example.com",  # arbitrary host — default deny (fail closed)
        "pastebin.com",
        # A remote MCP server on a non-allowlisted host is denied (P1.7): the in-sandbox MCP
        # client egresses through this proxy, so an MCP server outside the allowlist can't be
        # reached — onboarding a new remote MCP server requires a deliberate squid entry.
        "mcp.evil-server.com",
        # AWS data-exfil vectors closed by dropping the blanket `.amazonaws.com` allow:
        "abc123.execute-api.us-east-1.amazonaws.com",  # API Gateway invoke (2-label, attacker acct)
        "my-queue.sqs.eu-west-1.amazonaws.com",  # SQS with a queue subdomain
        "ec2.us-west-2.amazonaws.com",  # control-plane service in a non-allowlisted region
        "dynamodb.ap-south-1.amazonaws.com",
        "123456789012.dkr.ecr.eu-west-1.amazonaws.com",  # ECR data-plane (multi-label)
    ],
)
def test_exfil_and_unknown_hosts_denied(host):
    assert _decide(host) == "deny"


def test_aws_egress_is_per_service_region_allowlist_not_blanket():
    # The blanket `.amazonaws.com` allow is GONE — it was a cross-account data-exfil channel.
    # AWS egress is now a per-service, per-region allowlist. Locked so a reintroduction of the
    # catch-all is a conscious, test-visible change rather than silent drift.
    assert "acl allowed_domains dstdomain .amazonaws.com" not in _SQUID_CONF.read_text()
    # In-region single-label control-plane service: allowed.
    assert _decide("dynamodb.eu-west-1.amazonaws.com") == "allow"
    # Same service in a non-allowlisted region, and an attacker-usable data-plane endpoint:
    # both denied (would have ridden the old blanket allow).
    assert _decide("dynamodb.us-west-2.amazonaws.com") == "deny"
    assert _decide("abc123.execute-api.us-east-1.amazonaws.com") == "deny"


def test_virtual_hosted_s3_is_denied():
    # Regression for the former exfil bypass: aws_s3_re used to anchor only PATH-style S3
    # (`^s3.<region>...`), so virtual-hosted-style (`<bucket>.s3...`, the AWS CLI/SDK default)
    # slipped past the deny and rode the broad `.amazonaws.com` allow — an anonymous-writable
    # bucket was a clean exfil channel. aws_s3_re now matches the `s3` label after a dot too,
    # so both styles are denied before any allow rule.
    assert _decide("my-bucket.s3.amazonaws.com") == "deny"
    assert _decide("my-bucket.s3.eu-west-1.amazonaws.com") == "deny"
    assert _decide("bucket.s3.dualstack.us-east-1.amazonaws.com") == "deny"
    # Non-S3 AWS services must remain unaffected by the widened deny.
    assert _decide("ec2.eu-west-1.amazonaws.com") == "allow"
    assert _decide("bedrock-runtime.eu-west-1.amazonaws.com") == "allow"
