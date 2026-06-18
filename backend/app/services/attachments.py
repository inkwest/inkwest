"""File attachments for conversations.

Files are written into the conversation's host workdir
(``<sandbox_sessions_root>/<sid>/workdir/``) — the same directory bind-mounted at
``/workdir`` in the toolbox — so the claude-cli agent can open them via
``@/workdir/<file>``. Sandbox/claude-cli only; the API providers have no workdir.

Pure helpers (slugify, dedup, validation, confined path resolution) live here so
they can be unit-tested without HTTP or a database.
"""

import os
import re

from app.core.config import get_settings
from app.services.sandbox import session_dir


class AttachmentError(Exception):
    """Validation failure (size/type/name). The route maps it to a 4xx."""


def _workdir(session_id: str) -> str:
    """Host path of the conversation's workdir (mounted at /workdir in the toolbox)."""
    return os.path.join(session_dir(session_id), "workdir")


def allowed_extensions() -> set[str]:
    """Configured allowlist as a set of lowercase, dot-less extensions."""
    raw = get_settings().attachment_allowed_extensions
    return {e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()}


# Anything outside [A-Za-z0-9._-] is collapsed — keeps names shell- and path-safe.
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_basename(original: str) -> str:
    """Slugify an uploaded filename to a safe workdir basename, preserving extension.

    Strips any directory component, collapses unsafe chars to '-', and never returns
    an empty or dot-only name. Does NOT guarantee uniqueness — see :func:`unique_name`.
    """
    base = os.path.basename(original or "").strip()
    stem, ext = os.path.splitext(base)
    stem = _SLUG_RE.sub("-", stem).strip("-.") or "file"
    ext = _SLUG_RE.sub("", ext.lower())  # ".png" etc.; drop a bare/odd separator
    return f"{stem}{ext}" if ext and ext != "." else stem


def unique_name(work_dir: str, name: str) -> str:
    """Return ``name``, or ``name-1``, ``name-2``… if it already exists in work_dir."""
    if not os.path.exists(os.path.join(work_dir, name)):
        return name
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(work_dir, f"{stem}-{i}{ext}")):
        i += 1
    return f"{stem}-{i}{ext}"


def validate_extension(filename: str) -> None:
    """Raise :class:`AttachmentError` if the extension is not in the allowlist."""
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext not in allowed_extensions():
        raise AttachmentError(f"File type '.{ext or '?'}' is not allowed")


def resolve_existing(session_id: str, filename: str) -> str:
    """Resolve a stored attachment's host path, confined to the workdir.

    Rejects any name carrying a path component and any resolved path that escapes the
    workdir (traversal / symlink). Raises :class:`AttachmentError` if missing.
    """
    # Attachments are flat basenames; reject separators and dot segments outright.
    if filename != os.path.basename(filename) or filename in ("", ".", ".."):
        raise AttachmentError("Invalid attachment name")
    work_dir = _workdir(session_id)
    root = os.path.realpath(work_dir)
    path = os.path.realpath(os.path.join(work_dir, filename))
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        raise AttachmentError("Attachment not found")
    return path


def write_attachment(
    session_id: str, original_name: str, data: bytes, content_type: str
) -> dict[str, object]:
    """Validate, slugify+dedup, and write one attachment into the workdir.

    Returns the descriptor dict persisted on the message and returned to the client:
    ``{filename, original_name, content_type, size}``.
    """
    settings = get_settings()
    if len(data) > settings.attachment_max_bytes:
        limit_mib = settings.attachment_max_bytes // (1024 * 1024)
        raise AttachmentError(f"File exceeds the {limit_mib} MiB limit")

    name = safe_basename(original_name)
    validate_extension(name)

    work_dir = _workdir(session_id)
    # Mirror sandbox.provision: world-writable dir so the toolbox (a different uid) can
    # read, and so uploads work even before the first turn provisions the sandbox.
    os.makedirs(work_dir, exist_ok=True)
    os.chmod(work_dir, 0o777)  # noqa: S103 — per-conversation dir, as in sandbox.provision
    name = unique_name(work_dir, name)
    path = os.path.join(work_dir, name)
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o644)  # world-readable — the toolbox runs as a different uid

    return {
        "filename": name,
        "original_name": os.path.basename(original_name or name),
        "content_type": content_type or "application/octet-stream",
        "size": len(data),
    }


def delete_attachment(session_id: str, filename: str) -> None:
    """Remove a stored attachment (path-confined). Raises if it doesn't exist."""
    os.remove(resolve_existing(session_id, filename))
