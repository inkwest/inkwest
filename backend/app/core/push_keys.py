"""
Web Push key and URL checks, shared by boot validation (VAPID) and the subscription API.

Browsers expose and expect keys as base64url; a malformed value fails only later, in the
browser's ``pushManager.subscribe`` or at send time, so callers reject it up front.
"""

import base64
import binascii
import re
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}")


def b64url_decode(value: str) -> bytes:
    """Decode base64url, padding optional (browsers omit it; some libraries add it)."""
    if not _B64URL_RE.fullmatch(value):
        raise ValueError("must be base64url")
    raw = value.rstrip("=")
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except binascii.Error as e:
        raise ValueError("must be base64url") from e


def is_p256_public_key(value: str) -> bool:
    """True when ``value`` is a base64url uncompressed P-256 point that lies on the curve."""
    try:
        raw = b64url_decode(value)
        # Length/prefix first: from_encoded_point also accepts compressed (0x02/0x03) points.
        if len(raw) != 65 or raw[0] != 0x04:
            return False
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        return False
    return True


def _is_clean(value: str) -> bool:
    # urlsplit silently drops tabs/newlines, so whitespace and control chars are rejected first.
    return value.isprintable() and not any(c.isspace() for c in value)


def is_https_url(value: str) -> bool:
    """True for an ASCII https URL with a host, a valid port if any, and no userinfo,
    whitespace or control characters."""
    # ASCII keeps max_length a byte bound too, under the btree index row limit.
    if not value.isascii() or not _is_clean(value):
        return False
    try:
        url = urlsplit(value)
        url.port  # noqa: B018 -- raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return False
    return url.scheme == "https" and bool(url.hostname) and "@" not in url.netloc


def is_vapid_subject(value: str) -> bool:
    """True for a ``mailto:<address>`` or ``https://<host>`` VAPID contact."""
    if value.startswith("mailto:"):
        return len(value) > len("mailto:") and _is_clean(value)
    return is_https_url(value)
