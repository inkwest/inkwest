"""Tests for multi-user conversation sharing — access layer + sharing endpoints."""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# get_session_role / check_session_owner — unit tests on the access helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_role_owner_skips_db(mock_user: MagicMock) -> None:
    from app.services.session_access import get_session_role

    session = MagicMock()
    session.id = uuid.uuid4()
    session.user_id = str(mock_user.id)
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=None)

    assert await get_session_role(session, mock_user, db) == "owner"
    db.scalar.assert_not_called()  # owner short-circuits — no shares lookup


@pytest.mark.asyncio
async def test_get_session_role_admin_is_readonly_viewer(mock_admin_user: MagicMock) -> None:
    """An admin viewing someone else's conversation (e.g. from the usage report) is
    read-only: role ``viewer``, never ``owner`` — they can watch but not drive turns."""
    from app.services.session_access import get_session_role

    session = MagicMock()
    session.id = uuid.uuid4()
    session.user_id = str(uuid.uuid4())  # someone else's session
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=None)  # admin holds no share

    assert await get_session_role(session, mock_admin_user, db) == "viewer"


@pytest.mark.asyncio
async def test_get_session_role_admin_on_own_session_is_owner(mock_admin_user: MagicMock) -> None:
    """An admin viewing their *own* conversation is still the owner (full control)."""
    from app.services.session_access import get_session_role

    session = MagicMock()
    session.id = uuid.uuid4()
    session.user_id = str(mock_admin_user.id)
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=None)

    assert await get_session_role(session, mock_admin_user, db) == "owner"
    db.scalar.assert_not_called()  # owner short-circuits — no shares lookup


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "editor"])
async def test_get_session_role_accepted_participant(mock_user: MagicMock, role: str) -> None:
    from app.services.session_access import get_session_role

    session = MagicMock()
    session.id = uuid.uuid4()
    session.user_id = str(uuid.uuid4())
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=role)

    assert await get_session_role(session, mock_user, db) == role


@pytest.mark.asyncio
async def test_get_session_role_none_without_share(mock_user: MagicMock) -> None:
    from app.services.session_access import get_session_role

    session = MagicMock()
    session.id = uuid.uuid4()
    session.user_id = str(uuid.uuid4())
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=None)  # no accepted share

    assert await get_session_role(session, mock_user, db) is None


def test_check_session_owner_rejects_participant(mock_user: MagicMock) -> None:
    from app.services.session_access import check_session_owner

    session = MagicMock()
    session.user_id = str(uuid.uuid4())  # not the caller
    with pytest.raises(HTTPException) as exc:
        check_session_owner(session, mock_user)
    assert exc.value.status_code == 403


def test_check_session_owner_allows_owner(mock_user: MagicMock) -> None:
    from app.services.session_access import check_session_owner

    session = MagicMock()
    session.user_id = str(mock_user.id)
    check_session_owner(session, mock_user)  # must not raise


def test_check_real_owner_rejects_admin_on_foreign_session(mock_admin_user: MagicMock) -> None:
    """Sharing (invite / public link) is real-owner-only: an admin inspector must NOT be able
    to re-share someone else's private conversation."""
    from app.services.session_access import check_real_owner, check_session_owner

    session = MagicMock()
    session.user_id = str(uuid.uuid4())  # not the admin
    with pytest.raises(HTTPException) as exc:
        check_real_owner(session, mock_admin_user)
    assert exc.value.status_code == 403
    # But the admin IS still admitted by the management (revoke/list) path.
    check_session_owner(session, mock_admin_user)  # must not raise


def test_check_real_owner_allows_real_owner(mock_user: MagicMock) -> None:
    from app.services.session_access import check_real_owner

    session = MagicMock()
    session.user_id = str(mock_user.id)
    check_real_owner(session, mock_user)  # must not raise


# ---------------------------------------------------------------------------
# HTTP-level: read access for participants, owner-only for mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_as_editor_participant(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """An accepted editor can read the conversation; it's flagged as shared-in."""
    mock_session_obj.user_id = str(uuid.uuid4())  # owned by someone else
    owner = MagicMock()
    owner.display_name = "Owner Person"
    mock_session_obj.user = owner

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.scalar = AsyncMock(return_value="editor")  # accepted editor share

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_owner"] is False
    assert data["shared_role"] == "editor"
    assert data["owner_display_name"] == "Owner Person"


@pytest.mark.asyncio
async def test_get_session_as_admin_is_readonly(
    admin_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """An admin opening someone else's conversation (e.g. via the usage report) gets
    a read-only payload: is_owner=False, shared_role=viewer — never owner."""
    mock_session_obj.user_id = str(uuid.uuid4())  # owned by someone else
    owner = MagicMock()
    owner.display_name = "Owner Person"
    mock_session_obj.user = owner
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.scalar = AsyncMock(return_value=None)  # admin holds no share → viewer fallback

    resp = await admin_client.get(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_owner"] is False
    assert data["shared_role"] == "viewer"


@pytest.mark.asyncio
async def test_get_session_owner_includes_share_count(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    """The owner's session payload carries share_count = people + active links."""
    mock_session_obj.user_id = str(mock_user.id)  # caller owns it
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    # get_session_role short-circuits (owner) → no role scalar; then 2 count scalars.
    mock_db.scalar = AsyncMock(side_effect=[2, 1])  # 2 people, 1 active link

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_owner"] is True
    assert data["share_count"] == 3


@pytest.mark.asyncio
async def test_get_session_non_participant_forbidden(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    mock_session_obj.user_id = str(uuid.uuid4())
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.scalar = AsyncMock(return_value=None)  # no share

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_session_by_editor_forbidden(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """Editors send messages but never change session settings — owner-only."""
    mock_session_obj.user_id = str(uuid.uuid4())
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.scalar = AsyncMock(return_value="editor")  # even an accepted editor

    resp = await authenticated_client.patch(
        f"/api/sessions/{mock_session_obj.id}", json={"title": "renamed"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_sessions_includes_shared(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """A session shared with the caller appears in the list flagged is_owner=False."""
    mock_session_obj.user_id = str(uuid.uuid4())  # owned by someone else
    owner = MagicMock()
    owner.display_name = "Owner Person"
    mock_session_obj.user = owner

    count_result = MagicMock()
    count_result.scalar.return_value = 1
    list_result = MagicMock()
    list_result.all.return_value = [(mock_session_obj, None)]
    roles_result = MagicMock()
    roles_result.all.return_value = [(mock_session_obj.id, "editor")]
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result, roles_result])

    resp = await authenticated_client.get("/api/sessions")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["is_owner"] is False
    assert item["shared_role"] == "editor"
    assert item["owner_display_name"] == "Owner Person"

    # The list query unions owned sessions with accepted shares.
    list_query = str(mock_db.execute.call_args_list[1].args[0]).lower()
    assert "session_shares" in list_query


# ---------------------------------------------------------------------------
# Sharing endpoints — invite / respond / list / revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shareable_users_excludes_self_and_roles(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    other = MagicMock()
    other.id = uuid.uuid4()
    other.display_name = "Other Person"
    other.email = "other@test.com"
    result = MagicMock()
    result.scalars.return_value.all.return_value = [other]
    mock_db.execute = AsyncMock(return_value=result)

    resp = await authenticated_client.get("/api/users")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["display_name"] == "Other Person"
    # Minimal projection — no roles / oidc_subject leak to the picker.
    assert "roles" not in data[0]
    assert "oidc_subject" not in data[0]


def _populate_new_share(obj: MagicMock) -> None:
    """db.refresh side effect: stamp the server-side id/created_at a mock won't set."""
    obj.id = uuid.uuid4()
    obj.created_at = datetime.now(UTC)
    obj.responded_at = None


@pytest.mark.asyncio
async def test_create_share_invites_and_notifies(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)  # caller owns it
    mock_session_obj.title = "Investigating crashes"

    target_id = uuid.uuid4()
    target = MagicMock()
    target.id = target_id
    target.is_active = True
    target.display_name = "Invitee"
    target.email = "invitee@test.com"

    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=session_result)
    mock_db.scalar = AsyncMock(side_effect=[target, None])  # target user, no existing share
    mock_db.refresh = AsyncMock(side_effect=_populate_new_share)

    with patch("app.api.routes.sharing.notify", new=AsyncMock()) as mock_notify:
        resp = await authenticated_client.post(
            f"/api/sessions/{mock_session_obj.id}/shares",
            json={"user_id": str(target_id)},
        )

    assert resp.status_code == 201
    data = resp.json()
    # Shares are always read-only now (no editor role).
    assert data["role"] == "viewer"
    assert data["status"] == "pending"
    assert data["user_id"] == str(target_id)
    assert data["display_name"] == "Invitee"

    # Invite popup rides the per-user channel, addressed to the invitee.
    mock_notify.assert_awaited_once()
    payload = mock_notify.await_args.args[0]
    assert payload["type"] == "share_invite"
    assert mock_notify.await_args.kwargs["user_id"] == str(target_id)


@pytest.mark.asyncio
async def test_create_share_non_owner_forbidden(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    mock_session_obj.user_id = str(uuid.uuid4())  # owned by someone else
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=session_result)

    resp = await authenticated_client.post(
        f"/api/sessions/{mock_session_obj.id}/shares",
        json={"user_id": str(uuid.uuid4()), "role": "editor"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_share_with_self_rejected(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=session_result)

    resp = await authenticated_client.post(
        f"/api/sessions/{mock_session_obj.id}/shares",
        json={"user_id": str(mock_user.id), "role": "editor"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_respond_accept_notifies_owner_and_self(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_user: MagicMock,
) -> None:
    share_id = uuid.uuid4()
    share = MagicMock()
    share.id = share_id
    share.user_id = str(mock_user.id)  # the caller is the invitee
    share.status = "pending"
    share.session_id = uuid.uuid4()

    session = MagicMock()
    session.user_id = str(uuid.uuid4())  # the owner
    mock_db.scalar = AsyncMock(side_effect=[share, session])

    with patch("app.api.routes.sharing.notify", new=AsyncMock()) as mock_notify:
        resp = await authenticated_client.post(
            f"/api/shares/{share_id}/respond", json={"accept": True}
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert share.status == "accepted"
    # Owner + invitee's other tabs.
    assert mock_notify.await_count == 2


@pytest.mark.asyncio
async def test_respond_by_non_invitee_404(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
) -> None:
    share = MagicMock()
    share.id = uuid.uuid4()
    share.user_id = str(uuid.uuid4())  # someone else's invitation
    share.status = "pending"
    mock_db.scalar = AsyncMock(return_value=share)

    resp = await authenticated_client.post(f"/api/shares/{share.id}/respond", json={"accept": True})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_respond_already_responded_409(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_user: MagicMock,
) -> None:
    share = MagicMock()
    share.id = uuid.uuid4()
    share.user_id = str(mock_user.id)
    share.status = "accepted"  # already acted on
    mock_db.scalar = AsyncMock(return_value=share)

    resp = await authenticated_client.post(
        f"/api/shares/{share.id}/respond", json={"accept": False}
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_revoke_share_notifies_revoked_user(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    revoked_id = uuid.uuid4()

    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=session_result)
    mock_db.scalar = AsyncMock(return_value=MagicMock())  # the share to delete

    with patch("app.api.routes.sharing.notify", new=AsyncMock()) as mock_notify:
        resp = await authenticated_client.delete(
            f"/api/sessions/{mock_session_obj.id}/shares/{revoked_id}"
        )

    assert resp.status_code == 204
    mock_db.delete.assert_awaited_once()
    payload = mock_notify.await_args.args[0]
    assert payload["type"] == "share_revoked"
    assert mock_notify.await_args.kwargs["user_id"] == str(revoked_id)


@pytest.mark.asyncio
async def test_leave_share_removes_own_access(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_user: MagicMock,
) -> None:
    """A participant can remove their own share (no owner check) and gets a
    session_left event — not share_revoked, so no 'access removed' popup."""
    mock_db.scalar = AsyncMock(return_value=MagicMock())  # the caller's own share

    with patch("app.api.routes.sharing.notify", new=AsyncMock()) as mock_notify:
        resp = await authenticated_client.delete(f"/api/sessions/{uuid.uuid4()}/share")

    assert resp.status_code == 204
    mock_db.delete.assert_awaited_once()
    payload = mock_notify.await_args.args[0]
    assert payload["type"] == "session_left"
    assert mock_notify.await_args.kwargs["user_id"] == str(mock_user.id)


@pytest.mark.asyncio
async def test_leave_share_not_participant_404(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.scalar = AsyncMock(return_value=None)  # caller has no share here
    resp = await authenticated_client.delete(f"/api/sessions/{uuid.uuid4()}/share")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_pending_invites(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
) -> None:
    share = MagicMock()
    share.id = uuid.uuid4()
    share.role = "viewer"
    share.created_at = datetime.now(UTC)
    session = MagicMock()
    session.id = uuid.uuid4()
    session.title = "Prod incident"
    inviter = MagicMock()
    inviter.display_name = "Boss"

    result = MagicMock()
    result.all.return_value = [(share, session, inviter)]
    mock_db.execute = AsyncMock(return_value=result)

    resp = await authenticated_client.get("/api/shares/pending")
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["session_title"] == "Prod incident"
    assert item["invited_by_display_name"] == "Boss"
    assert item["role"] == "viewer"


@pytest.mark.asyncio
async def test_list_shares_returns_participants(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)

    share = MagicMock()
    share.id = uuid.uuid4()
    share.session_id = mock_session_obj.id
    share.role = "editor"
    share.status = "accepted"
    share.created_at = datetime.now(UTC)
    share.responded_at = datetime.now(UTC)
    participant = MagicMock()
    participant.id = uuid.uuid4()
    participant.display_name = "Participant"
    participant.email = "p@test.com"

    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    join_result = MagicMock()
    join_result.all.return_value = [(share, participant)]
    mock_db.execute = AsyncMock(side_effect=[session_result, join_result])

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/shares")
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["display_name"] == "Participant"
    assert item["status"] == "accepted"
    assert item["role"] == "editor"


# ---------------------------------------------------------------------------
# WebSocket access (participants), presence/typing, authorship
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["editor", "viewer"])
async def test_validate_session_allows_accepted_participant(role: str) -> None:
    from app.api.routes.chat import validate_session

    session = MagicMock()
    session.cluster_id = None  # no Kube context needed for this check
    session.user_id = "owner-id"
    user = MagicMock()
    user.id = "participant-id"
    user.is_admin = lambda: False

    db = AsyncMock()
    db.get = AsyncMock(return_value=session)
    db.scalar = AsyncMock(return_value=role)  # accepted share of this role

    sess, cluster, resolved_role, error = await validate_session(db, "sid", user)
    assert error is None
    assert sess is session
    assert resolved_role == role


@pytest.mark.asyncio
async def test_broadcast_to_session_excludes_sender() -> None:
    from app.api.routes.chat import active_connections, broadcast_to_session

    sid = "sess-broadcast"
    sender = MagicMock()
    sender.send_json = AsyncMock()
    peer = MagicMock()
    peer.send_json = AsyncMock()
    active_connections[sid] = {sender, peer}
    try:
        await broadcast_to_session(sid, {"type": "user_message"}, exclude=sender)
    finally:
        active_connections.pop(sid, None)

    sender.send_json.assert_not_called()
    peer.send_json.assert_awaited_once()
    assert peer.send_json.await_args.args[0] == {"type": "user_message"}


@pytest.mark.asyncio
async def test_presence_roster_dedupes_by_user() -> None:
    from app.api.routes.chat import _register_presence, active_connections, session_presence

    sid = "sess-presence"
    tab_a1 = MagicMock()
    tab_a1.send_json = AsyncMock()
    tab_a2 = MagicMock()
    tab_a2.send_json = AsyncMock()
    tab_b = MagicMock()
    tab_b.send_json = AsyncMock()
    active_connections[sid] = {tab_a1, tab_a2, tab_b}

    alice = MagicMock()
    alice.id = "alice"
    alice.display_name = "Alice"
    bob = MagicMock()
    bob.id = "bob"
    bob.display_name = "Bob"

    try:
        await _register_presence(sid, tab_a1, alice)
        await _register_presence(sid, tab_a2, alice)  # second tab, same user
        await _register_presence(sid, tab_b, bob)
        roster = tab_b.send_json.await_args.args[0]
    finally:
        session_presence.pop(sid, None)
        active_connections.pop(sid, None)

    assert roster["type"] == "presence"
    # Alice appears once despite two tabs.
    assert sorted(u["user_id"] for u in roster["users"]) == ["alice", "bob"]


@pytest.mark.asyncio
async def test_send_to_user_in_session_targets_only_that_user() -> None:
    """The MCP tool-ask prompt must reach only the asking user's tabs — never a peer's
    or a guest's (guests carry synthetic guest:* presence ids)."""
    from app.api.routes.chat import Presence, send_to_user_in_session, session_presence

    sid = "sess-ask"
    tab_mine = MagicMock()
    tab_mine.send_json = AsyncMock()
    tab_mine_dead = MagicMock()
    tab_mine_dead.send_json = AsyncMock(side_effect=RuntimeError("closed"))
    tab_peer = MagicMock()
    tab_peer.send_json = AsyncMock()
    tab_guest = MagicMock()
    tab_guest.send_json = AsyncMock()
    session_presence[sid] = {
        tab_mine: Presence("me", "Me"),
        tab_mine_dead: Presence("me", "Me"),
        tab_peer: Presence("peer", "Peer"),
        tab_guest: Presence("guest:1", "Guest"),
    }
    try:
        sent = await send_to_user_in_session(sid, "me", {"type": "mcp_tool_ask"})
    finally:
        session_presence.pop(sid, None)

    assert sent == 1  # dead socket skipped, still counted only when the send succeeded
    tab_mine.send_json.assert_awaited_once()
    tab_peer.send_json.assert_not_called()
    tab_guest.send_json.assert_not_called()

    # No socket at all for the user → 0, the proxy's immediate-deny signal.
    assert await send_to_user_in_session("no-such-session", "me", {}) == 0


@pytest.mark.asyncio
async def test_close_user_session_connections_targets_one_user() -> None:
    from app.api.routes.chat import Presence, close_user_session_connections, session_presence

    sid = "sess-kick"
    ws_target = MagicMock()
    ws_target.close = AsyncMock()
    ws_other = MagicMock()
    ws_other.close = AsyncMock()
    session_presence[sid] = {
        ws_target: Presence("revoked-user", "Revoked"),
        ws_other: Presence("staying-user", "Staying"),
    }
    try:
        await close_user_session_connections(sid, "revoked-user")
    finally:
        session_presence.pop(sid, None)

    ws_target.close.assert_awaited_once()
    ws_other.close.assert_not_called()


@pytest.mark.asyncio
async def test_save_message_records_author() -> None:
    from app.services.messages import save_message

    db = AsyncMock()
    db.add = MagicMock()
    author = uuid.uuid4()
    await save_message(db, uuid.uuid4(), "user", "why is it down?", author_user_id=author)

    saved = db.add.call_args.args[0]
    assert saved.role == "user"
    assert saved.author_user_id == author


# ---------------------------------------------------------------------------
# Revokable anonymous share links
# ---------------------------------------------------------------------------


def _populate_new_link(obj: MagicMock) -> None:
    obj.id = uuid.uuid4()
    obj.created_at = datetime.now(UTC)
    obj.is_active = True


@pytest.mark.asyncio
async def test_create_share_link(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=session_result)
    mock_db.refresh = AsyncMock(side_effect=_populate_new_link)

    resp = await authenticated_client.post(
        f"/api/sessions/{mock_session_obj.id}/share-links", json={"label": "for Bob"}
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["token"]
    assert data["url"] == f"/shared/{data['token']}"
    assert data["label"] == "for Bob"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_list_share_links(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj

    link = MagicMock()
    link.id = uuid.uuid4()
    link.token = "tok123"
    link.label = None
    link.is_active = True
    link.created_at = datetime.now(UTC)
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [link]
    mock_db.execute = AsyncMock(side_effect=[session_result, list_result])

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/share-links")
    assert resp.status_code == 200
    assert resp.json()[0]["url"] == "/shared/tok123"


@pytest.mark.asyncio
async def test_revoke_share_link_kicks_guests(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=session_result)

    link = MagicMock()
    link.token = "tok-x"
    link.is_active = True
    mock_db.scalar = AsyncMock(return_value=link)

    with patch("app.api.routes.sharing.close_share_link_connections", new=AsyncMock()) as kick:
        resp = await authenticated_client.delete(
            f"/api/sessions/{mock_session_obj.id}/share-links/{uuid.uuid4()}"
        )

    assert resp.status_code == 204
    assert link.is_active is False  # revoked, kept for audit
    kick.assert_awaited_once_with("tok-x")


@pytest.mark.asyncio
async def test_get_shared_conversation_excludes_system(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    sid = uuid.uuid4()
    sys_msg = SimpleNamespace(
        role="system",
        content={"type": "session_event"},
        id=uuid.uuid4(),
        session_id=sid,
        created_at=datetime.now(UTC),
    )
    user_msg = SimpleNamespace(
        role="user",
        content="why is it down?",
        id=uuid.uuid4(),
        session_id=sid,
        created_at=datetime.now(UTC),
    )
    session = SimpleNamespace(
        id=sid,
        title="Shared chat",
        user=SimpleNamespace(display_name="Owner"),
        messages=[sys_msg, user_msg],
    )

    mock_db.scalar = AsyncMock(return_value=SimpleNamespace(session_id=sid))  # active link
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = session
    mock_db.execute = AsyncMock(return_value=session_result)

    resp = await authenticated_client.get("/api/shared/some-token")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Shared chat"
    assert data["owner_display_name"] == "Owner"
    # The system event line is stripped from the public transcript.
    assert len(data["messages"]) == 1
    assert data["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_get_shared_conversation_invalid_token_404(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.scalar = AsyncMock(return_value=None)  # no active link
    resp = await authenticated_client.get("/api/shared/revoked-or-bad")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_close_share_link_connections() -> None:
    from app.api.routes.chat import close_share_link_connections, shared_link_connections

    ws = MagicMock()
    ws.close = AsyncMock()
    shared_link_connections["tok-kill"] = {ws}
    try:
        await close_share_link_connections("tok-kill")
    finally:
        shared_link_connections.pop("tok-kill", None)
    ws.close.assert_awaited_once()


def _session_cm(db: object) -> MagicMock:
    """Mock for `async with async_session_maker() as db`."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["unknown_link", "guest_cap"])
async def test_websocket_shared_refusals_accept_before_close(refusal: str) -> None:
    """A close BEFORE accept leaves as HTTP 403 and reaches the browser as 1006, so the
    client cannot tell a revoked link / full link from a dead backend and keeps retrying.
    """
    from app.api.routes import chat

    ws = AsyncMock()
    db = AsyncMock()
    if refusal == "unknown_link":
        db.scalar = AsyncMock(return_value=None)
        expected = 4004
    else:
        link = MagicMock(session_id="sess-1")
        db.scalar = AsyncMock(return_value=link)
        db.get = AsyncMock(return_value=MagicMock())
        expected = 4009
    with (
        patch.object(chat, "async_session_maker", _session_cm(db)),
        patch.object(chat, "connect", AsyncMock(return_value=False)),
    ):
        await chat.websocket_shared(ws, "tok")

    assert ws.method_calls == [
        call.accept(subprotocol="inkwest"),
        call.close(code=expected),
    ]


# ---------------------------------------------------------------------------
# turn_runner.wait_for_turn — passive viewers follow turns that start later
# ---------------------------------------------------------------------------


async def _empty_orchestrate(**kwargs):
    # An async generator that yields nothing (a turn with no events).
    return
    yield  # pragma: no cover — unreachable, only makes this an async generator


@pytest.mark.asyncio
async def test_wait_for_turn_returns_immediately_when_active() -> None:
    from app.services.turn_runner import TurnRunner, _Turn

    tr = TurnRunner()
    tr._turns["s"] = _Turn(session_id="s", cancel_event=asyncio.Event())
    # A turn is already active → must not block (closes the subscribe race).
    await asyncio.wait_for(tr.wait_for_turn("s"), timeout=1)


@pytest.mark.asyncio
async def test_wait_for_turn_resolves_when_a_turn_starts() -> None:
    from app.services.turn_runner import TurnRunner

    tr = TurnRunner()
    waiter = asyncio.create_task(tr.wait_for_turn("s"))
    await asyncio.sleep(0)  # let the waiter register
    assert not waiter.done()  # no turn yet → still blocked

    started = await tr.start("s", _empty_orchestrate, {})
    assert started
    # The starting turn must wake the waiter so its follow loop can subscribe.
    await asyncio.wait_for(waiter, timeout=1)


# ---------------------------------------------------------------------------
# Dev-mode user switching (test multi-user sharing without an IdP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_dev_user_impersonation_is_non_admin(mock_db: AsyncMock) -> None:
    from app.api.dependencies import get_or_create_dev_user

    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # not yet created
    mock_db.execute = AsyncMock(return_value=result)
    mock_db.add = MagicMock()

    await get_or_create_dev_user(mock_db, email="bob@localhost", display_name="bob", admin=False)

    created = mock_db.add.call_args.args[0]
    assert created.email == "bob@localhost"
    assert created.oidc_subject == "dev-bob"
    assert created.roles == []  # impersonated users are plain (non-admin)


@pytest.mark.asyncio
async def test_dev_switch_sets_and_clears_cookie(authenticated_client: AsyncClient) -> None:
    with patch("app.api.routes.auth.get_settings") as mock_settings:
        settings = MagicMock()
        settings.oidc_enabled = False
        settings.app_env = "development"
        mock_settings.return_value = settings

        resp = await authenticated_client.get("/api/auth/dev-switch?user=bob")
        assert resp.status_code == 200
        assert resp.json()["acting_as"] == "bob@localhost"
        assert resp.cookies.get("inkwest_dev_user") == "bob"

        cleared = await authenticated_client.get("/api/auth/dev-switch")
        assert cleared.json()["acting_as"] == "dev@localhost"


@pytest.mark.asyncio
async def test_dev_switch_404_under_oidc(authenticated_client: AsyncClient) -> None:
    with patch("app.api.routes.auth.get_settings") as mock_settings:
        settings = MagicMock()
        settings.oidc_enabled = True  # production-style — the switch must not exist
        mock_settings.return_value = settings

        resp = await authenticated_client.get("/api/auth/dev-switch?user=bob")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Anonymous share-link live-event sanitization (_sanitize_shared_event)
#
# The public viewer's live socket must mirror the narrow REST projection: it may
# see the answer text and tool I/O, but never internal reasoning or per-turn
# token/cost metadata. Whitelist semantics — unknown types are withheld.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "thinking", "content": "secret chain-of-thought"},
        {"type": "reasoning", "content": "deciding what to do next"},
        {"type": "status_update", "metadata": {"total_tokens": 1234}},
        {"type": "budget_warning", "content": "Budget 'team': 80% used"},
        {"type": "tool_proposal", "tool_name": "kubectl", "tool_input": {}},
        {"type": "search_proposal", "content": "why I want to search"},
        {"type": "confirm_request", "content": "approve?"},
        {"type": "session_info", "metadata": {"cli_session_id": "abc"}},
        {"type": "connector_required", "metadata": {"connector": "aws"}},
        {"type": "mcp_auth_required", "metadata": {"mcp_key": "x"}},
        {"type": "error", "content": "boto3 AccessDenied for arn:aws:iam::123"},
        {"type": "search_results", "metadata": {"results": [{"url": "x"}]}},
        {"type": "some_future_event", "content": "unknown"},
    ],
)
def test_sanitize_shared_event_drops_internal_events(payload: dict) -> None:
    from app.api.routes.chat import _sanitize_shared_event

    assert _sanitize_shared_event(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "text_delta", "content": "the answer"},
        {"type": "tool_start", "tool_name": "kubectl", "tool_input": {"args": "get pods"}},
        {"type": "tool_result", "tool_name": "kubectl", "content": "pod/foo Running"},
    ],
)
def test_sanitize_shared_event_passes_presentational_events(payload: dict) -> None:
    from app.api.routes.chat import _sanitize_shared_event

    # Forwarded verbatim — these are exactly what the REST transcript already shows.
    assert _sanitize_shared_event(payload) == payload


def test_sanitize_shared_event_strips_done_metadata() -> None:
    from app.api.routes.chat import _sanitize_shared_event

    # The authenticated `done` carries token/iteration/latency metadata the public
    # viewer must not see; only the `stopped` lifecycle flag survives.
    out = _sanitize_shared_event(
        {
            "type": "done",
            "metadata": {"input_tokens": 100, "output_tokens": 50, "iterations": 3},
        }
    )
    assert out == {"type": "done"}

    stopped = _sanitize_shared_event(
        {"type": "done", "metadata": {"stopped": True, "output_tokens": 50}}
    )
    assert stopped == {"type": "done", "metadata": {"stopped": True}}


# ---------------------------------------------------------------------------
# Anonymous share-link broadcast sanitization (_sanitize_guest_broadcast)
#
# The session-wide broadcast path (presence, typing, a peer's message echo) must
# not leak authenticated participants' identities to a public guest socket.
# ---------------------------------------------------------------------------


def test_sanitize_guest_broadcast_strips_user_message_author() -> None:
    from app.api.routes.chat import _sanitize_guest_broadcast

    # The guest must see the live question, but never who authored it.
    out = _sanitize_guest_broadcast(
        {
            "type": "user_message",
            "content": "what's failing?",
            "attachments": [{"name": "log.txt"}],
            "author_user_id": "11111111-1111-1111-1111-111111111111",
            "author_display_name": "Alice",
        }
    )
    # Exact-equality already proves no author_user_id / author_display_name survived.
    assert out == {
        "type": "user_message",
        "content": "what's failing?",
        "attachments": [{"name": "log.txt"}],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "presence", "users": [{"user_id": "u", "display_name": "Alice"}]},
        {"type": "typing", "user_id": "u", "display_name": "Alice", "state": True},
        {"type": "some_future_broadcast", "user_id": "u"},
    ],
)
def test_sanitize_guest_broadcast_drops_identity_frames(payload: dict) -> None:
    from app.api.routes.chat import _sanitize_guest_broadcast

    assert _sanitize_guest_broadcast(payload) is None


@pytest.mark.asyncio
async def test_pump_turn_sanitizes_guest_stream() -> None:
    """Wiring: _pump_turn applies the sanitizer to both the replayed buffer and live queue."""
    from app.api.routes import chat

    ws = MagicMock()
    ws.send_json = AsyncMock()
    queue: asyncio.Queue = asyncio.Queue()
    snapshot = [
        {"type": "thinking", "content": "secret chain-of-thought"},
        {"type": "text_delta", "content": "hello"},
    ]
    await queue.put({"type": "done", "metadata": {"input_tokens": 99}})
    await queue.put(chat.TURN_END)

    with patch.object(chat.turn_runner, "unsubscribe", new=AsyncMock()):
        await chat._pump_turn(ws, "sid", snapshot, queue, chat._sanitize_shared_event)

    sent = [c.args[0] for c in ws.send_json.call_args_list]
    assert {"type": "text_delta", "content": "hello"} in sent
    assert {"type": "done"} in sent  # token metadata stripped
    assert all(p.get("type") != "thinking" for p in sent)  # reasoning withheld


@pytest.mark.asyncio
async def test_broadcast_to_session_sanitizes_only_guest_sockets() -> None:
    """Wiring: a guest socket on a session gets the sanitized projection; members get raw."""
    from app.api.routes import chat

    guest = MagicMock()
    guest.send_json = AsyncMock()
    member = MagicMock()
    member.send_json = AsyncMock()
    sid = "broadcast-test-session"

    chat.active_connections[sid].update({guest, member})
    chat.guest_connections.add(guest)
    try:
        # presence: dropped for the guest, delivered verbatim to the member.
        await chat.broadcast_to_session(
            sid, {"type": "presence", "users": [{"user_id": "u", "display_name": "Alice"}]}
        )
        guest.send_json.assert_not_called()
        member.send_json.assert_awaited_once()

        # user_message: stripped of author for the guest, full for the member.
        guest.send_json.reset_mock()
        member.send_json.reset_mock()
        await chat.broadcast_to_session(
            sid,
            {
                "type": "user_message",
                "content": "hi",
                "attachments": [],
                "author_user_id": "u",
                "author_display_name": "Alice",
            },
        )
        assert guest.send_json.call_args.args[0] == {
            "type": "user_message",
            "content": "hi",
            "attachments": [],
        }
        assert member.send_json.call_args.args[0]["author_display_name"] == "Alice"
    finally:
        chat.guest_connections.discard(guest)
        chat.active_connections.pop(sid, None)
