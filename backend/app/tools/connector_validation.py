"""
Validation for connector tool inputs.

Note on AWS: there is intentionally **no read-only allowlist** here. Least privilege
for AWS is enforced by the IAM/SSO role the user selects when granting access (a
read-only role), not by filtering operations in code. This validation only does
basic argv hygiene — the command is run as a list (no shell), so there is no
injection risk; we just reject malformed input.
"""

import re
from typing import Any

_AWS_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_AWS_OPERATION = re.compile(r"^[a-z][a-z0-9-]{1,60}$")

# Global AWS CLI flags an untrusted (prompt-injectable) tool call must never set. These
# subvert the read-only/allowlisted boundary rather than parameterize a read:
#   --endpoint-url   redirect the signed call to an attacker/SSRF host
#   --no-verify-ssl / --no-paginate/ca-bundle   strip TLS / alter transport
#   --profile        pick another credential profile (we inject the session's own)
#   --debug          dumps request internals (incl. credentials) into the returned output
_FORBIDDEN_AWS_FLAGS = (
    "--endpoint-url",
    "--endpoint",
    "--no-verify-ssl",
    "--ca-bundle",
    "--debug",
    "--profile",
)


def validate_aws_call(tool_input: dict[str, Any]) -> str | None:
    """Return an error message if the aws_call input is malformed or uses a forbidden flag.

    Not a read-only *policy* check (that is the IAM/SSO role's job), but it does block the
    handful of global CLI flags/values that would escape the intended boundary: endpoint
    redirection (SSRF), TLS bypass, profile switching, and ``file://`` parameter values that
    make the CLI read local files (e.g. the mounted credentials) into the request/output.
    """
    service = str(tool_input.get("service", "")).strip().lower()
    operation = str(tool_input.get("operation", "")).strip().lower()

    if not _AWS_SERVICE.match(service):
        return f"Invalid AWS service: {service!r}"
    if not _AWS_OPERATION.match(operation):
        return f"Invalid AWS operation: {operation!r}"

    args = tool_input.get("args", [])
    if args is not None:
        if not isinstance(args, list):
            return "args must be a list of strings"
        for a in args:
            if not isinstance(a, str):
                return "each arg must be a string"
            if "\n" in a or "\r" in a:
                return "args must not contain newlines"
            flag = a.strip().lower().split("=", 1)[0]
            if flag in _FORBIDDEN_AWS_FLAGS:
                return f"AWS flag not allowed: {flag}"
            # `file://` / `fileb://` make the AWS CLI splice a local file into a parameter
            # value — a read of arbitrary container files (e.g. ~/.aws/credentials).
            if "file://" in a.lower():
                return "file:// argument values are not allowed"
    return None


def validate_gitlab_api(tool_input: dict[str, Any]) -> str | None:
    """Return an error message if the gitlab_api input is not a safe GET path."""
    path = str(tool_input.get("path", "")).strip()
    if not path.startswith("/"):
        return "path must start with '/' (relative to /api/v4)"
    if "://" in path or path.startswith("//"):
        return "absolute URLs are not allowed"
    if ".." in path:
        return "path must not contain '..'"
    return None
