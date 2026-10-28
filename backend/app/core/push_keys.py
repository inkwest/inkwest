"""
Web Push key and URL checks, shared by boot validation (VAPID) and the subscription API.

Browsers expose and expect keys as base64url; a malformed value fails only later, in the
browser's ``pushManager.subscribe`` or at send time, so callers reject it up front.
"""

import base64
import binascii
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

if TYPE_CHECKING:
    from py_vapid import Vapid

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}")

# The backend POSTs to user-supplied endpoints: only browser push services are reachable.
# Leading dot = any subdomain, else exact host.
PUSH_SERVICE_HOSTS = (
    "fcm.googleapis.com",
    ".push.services.mozilla.com",
    ".notify.windows.com",
    ".push.apple.com",
)


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


# AC2 shape; py_vapid's own check leaves the mailto: tail open and reads dead:1234 as IPv6.
_VAPID_SUBJECT_RE = re.compile(
    r"mailto:[A-Za-z0-9.!#$%&'*+=^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+"
    r"|https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+"
)


def is_vapid_subject(value: str) -> bool:
    """True for exactly ``mailto:<user>@<domain>`` or ``https://<domain>``, nothing after it."""
    return _VAPID_SUBJECT_RE.fullmatch(value) is not None


def _is_clean_ascii(value: str) -> bool:
    """True for printable ASCII with no whitespace."""
    # urlsplit silently drops tabs/newlines, so whitespace and control chars are rejected first.
    return value.isascii() and value.isprintable() and not any(c.isspace() for c in value)


def is_https_url(value: str) -> bool:
    """True for an ASCII https URL with a host, a valid port if any, and no userinfo,
    whitespace or control characters."""
    # ASCII keeps max_length a byte bound too, under the btree index row limit.
    if not _is_clean_ascii(value):
        return False
    try:
        url = urlsplit(value)
        url.port  # noqa: B018 -- raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return False
    return url.scheme == "https" and bool(url.hostname) and "@" not in url.netloc


def is_push_service_url(value: str) -> bool:
    """True when the URL's host is one of ``PUSH_SERVICE_HOSTS``."""
    host = urlsplit(value).hostname or ""
    return any(
        host.endswith(allowed) if allowed.startswith(".") else host == allowed
        for allowed in PUSH_SERVICE_HOSTS
    )


def load_vapid(private_key: str, public_key: str) -> "Vapid":
    """Parse ``private_key`` as pywebpush does and check it derives ``public_key``.

    Raises ValueError without the key or the parser's message in it.
    """
    # Local: config imports this module, and the lean agent image ships no py_vapid.
    from py_vapid import Vapid

    try:
        vapid = Vapid.from_string(private_key)
        derived = vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    except Exception:
        raise ValueError("VAPID_PRIVATE_KEY is not a base64url raw or DER P-256 key") from None
    if derived != b64url_decode(public_key):
        raise ValueError("VAPID_PRIVATE_KEY does not derive VAPID_PUBLIC_KEY")
    return vapid
