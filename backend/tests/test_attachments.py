"""Tests for conversation file attachments (service helpers + API endpoints)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.services import attachments, sandbox


@pytest.fixture
def workdir_root(tmp_path, monkeypatch):
    """Point the attachment workdir resolver at a throwaway directory."""
    # sandbox owns the layout; attachments resolves through sandbox.workdir_path.
    monkeypatch.setattr(sandbox, "session_dir", lambda sid: str(tmp_path / sid))
    return tmp_path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("report.PNG", "report.png"),  # extension lowercased
        ("my report (1).txt", "my-report-1.txt"),  # spaces/parens collapsed
        ("/etc/passwd", "passwd"),  # directory component stripped
        ("../../escape.log", "escape.log"),  # traversal stripped to basename
        ("   ", "file"),  # blank -> safe fallback
        ("évidence.json", "vidence.json"),  # non-ascii collapsed, ext kept
    ],
)
def test_safe_basename(original, expected):
    assert attachments.safe_basename(original) == expected


def test_unique_name_dedupes(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert attachments.unique_name(str(tmp_path), "a.txt") == "a-1.txt"
    (tmp_path / "a-1.txt").write_text("x")
    assert attachments.unique_name(str(tmp_path), "a.txt") == "a-2.txt"
    assert attachments.unique_name(str(tmp_path), "fresh.txt") == "fresh.txt"


def test_validate_extension_rejects_disallowed():
    attachments.validate_extension("ok.txt")  # no raise
    with pytest.raises(attachments.AttachmentError):
        attachments.validate_extension("payload.exe")


def test_write_attachment_writes_file_and_descriptor(workdir_root):
    desc = attachments.write_attachment("sess1", "Notes.txt", b"hello", "text/plain")
    assert desc == {
        "filename": "Notes.txt",
        "original_name": "Notes.txt",
        "content_type": "text/plain",
        "size": 5,
    }
    written = workdir_root / "sess1" / "workdir" / "Notes.txt"
    assert written.read_bytes() == b"hello"
    # World-readable so the toolbox (a different uid) can read it.
    assert (written.stat().st_mode & 0o004) != 0


def test_write_attachment_dedupes_on_collision(workdir_root):
    first = attachments.write_attachment("s", "a.txt", b"1", "text/plain")
    second = attachments.write_attachment("s", "a.txt", b"2", "text/plain")
    assert first["filename"] == "a.txt"
    assert second["filename"] == "a-1.txt"


def test_write_attachment_rejects_oversized(workdir_root, monkeypatch):
    monkeypatch.setattr(get_settings(), "attachment_max_bytes", 4)
    with pytest.raises(attachments.AttachmentError, match="exceeds"):
        attachments.write_attachment("s", "a.txt", b"toolong", "text/plain")


def test_write_attachment_rejects_bad_type(workdir_root):
    with pytest.raises(attachments.AttachmentError):
        attachments.write_attachment("s", "evil.exe", b"x", "application/octet-stream")


@pytest.mark.parametrize("bad", ["../escape.txt", "a/b.txt", "..", "", "."])
def test_resolve_existing_rejects_traversal(workdir_root, bad):
    with pytest.raises(attachments.AttachmentError):
        attachments.resolve_existing("s", bad)


def test_resolve_and_delete_roundtrip(workdir_root):
    attachments.write_attachment("s", "a.txt", b"x", "text/plain")
    path = attachments.resolve_existing("s", "a.txt")
    assert path.endswith("/workdir/a.txt")
    attachments.delete_attachment("s", "a.txt")
    with pytest.raises(attachments.AttachmentError):
        attachments.resolve_existing("s", "a.txt")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def _owned_session(mock_db: AsyncMock, mock_session_obj: MagicMock) -> MagicMock:
    """Make _load_owned_session resolve to the caller's own session."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)
    return mock_session_obj


@pytest.fixture
def attachments_enabled(workdir_root, monkeypatch):
    """Enable the feature (the sandbox) and confine writes to tmp."""
    s = get_settings()
    monkeypatch.setattr(s, "sandbox_enabled", True)
    monkeypatch.setattr(s, "sandbox_sessions_root", str(workdir_root))


@pytest.mark.asyncio
async def test_upload_rejected_when_sandbox_disabled(
    authenticated_client: AsyncClient, _owned_session: MagicMock, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "sandbox_enabled", False)
    resp = await authenticated_client.post(
        f"/api/sessions/{_owned_session.id}/attachments",
        files={"files": ("a.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_upload_then_get_roundtrip(
    authenticated_client: AsyncClient,
    _owned_session: MagicMock,
    attachments_enabled,
) -> None:
    resp = await authenticated_client.post(
        f"/api/sessions/{_owned_session.id}/attachments",
        files={"files": ("note.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200
    [desc] = resp.json()
    assert desc["filename"] == "note.txt"
    assert desc["size"] == 11

    got = await authenticated_client.get(
        f"/api/sessions/{_owned_session.id}/attachments/{desc['filename']}"
    )
    assert got.status_code == 200
    assert got.content == b"hello world"


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_type(
    authenticated_client: AsyncClient, _owned_session: MagicMock, attachments_enabled
) -> None:
    resp = await authenticated_client.post(
        f"/api/sessions/{_owned_session.id}/attachments",
        files={"files": ("evil.exe", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_attachment_rejects_traversal(
    authenticated_client: AsyncClient, _owned_session: MagicMock, attachments_enabled
) -> None:
    # A traversal segment can't reach the route via a clean path, but a missing
    # flat name must 404 (not 200) — the resolver confines to the workdir.
    resp = await authenticated_client.get(f"/api/sessions/{_owned_session.id}/attachments/nope.txt")
    assert resp.status_code == 404
