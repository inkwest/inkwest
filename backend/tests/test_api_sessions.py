"""Tests for session API routes."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_sessions_empty(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_sessions_returns_items(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    list_result = MagicMock()
    list_result.all.return_value = [(mock_session_obj, None)]
    share_agg = MagicMock()
    share_agg.all.return_value = []  # owned session: no shares / links
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result, share_agg, share_agg])

    resp = await authenticated_client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "Test Session"
    assert data["items"][0]["share_count"] == 0


@pytest.mark.asyncio
async def test_list_sessions_owned_share_count(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """An owned conversation's list entry carries share_count = non-declined people +
    active links, so the sidebar can flag shared conversations without opening them."""
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    list_result = MagicMock()
    list_result.all.return_value = [(mock_session_obj, None)]
    people_agg = MagicMock()
    people_agg.all.return_value = [(mock_session_obj.id, 2)]  # 2 invited/accepted
    link_agg = MagicMock()
    link_agg.all.return_value = [(mock_session_obj.id, 1)]  # 1 active link
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result, people_agg, link_agg])

    resp = await authenticated_client.get("/api/sessions")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["is_owner"] is True
    assert item["share_count"] == 3


@pytest.mark.asyncio
async def test_list_sessions_populates_last_message_at(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """The list surfaces the newest message time (joined MAX) so the client can sort
    by activity; None when the session has no messages."""
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    list_result = MagicMock()
    list_result.all.return_value = [(mock_session_obj, datetime(2026, 1, 2, 3, 4, 5))]
    share_agg = MagicMock()
    share_agg.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result, share_agg, share_agg])

    resp = await authenticated_client.get("/api/sessions?sort=activity&order=desc")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["last_message_at"].startswith("2026-01-02T03:04:05")


@pytest.mark.asyncio
async def test_list_sessions_activity_sort_orders_by_last_message(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """sort=activity orders by coalesce(last_message_at, created_at)."""
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get("/api/sessions?sort=activity&order=asc")
    assert resp.status_code == 200
    list_query = str(mock_db.execute.call_args_list[1].args[0]).lower()
    assert "coalesce" in list_query
    assert "max(" in list_query


@pytest.mark.asyncio
async def test_list_sessions_shared_by_me_filter_builds_exists(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """share=shared_by_me narrows to owned sessions that have a participant or link."""
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get("/api/sessions?share=shared_by_me")
    assert resp.status_code == 200
    # The filter is applied before the count so pagination reflects it.
    count_query = str(mock_db.execute.call_args_list[0].args[0]).lower()
    assert "exists" in count_query
    assert "session_shares" in count_query
    assert "session_share_links" in count_query


@pytest.mark.asyncio
async def test_list_sessions_rejects_unknown_share_filter(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await authenticated_client.get("/api/sessions?share=bogus")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_sessions_activity_after_filters_by_cutoff(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """activity_after keeps only sessions active at/after the cutoff, before the count."""
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get("/api/sessions?activity_after=2026-07-01T00:00:00Z")
    assert resp.status_code == 200
    count_query = str(mock_db.execute.call_args_list[0].args[0]).lower()
    assert "coalesce" in count_query
    assert ">=" in count_query


@pytest.mark.asyncio
async def test_list_sessions_custom_range_bounds_both_ends(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """A custom from–to window applies both activity_after (>=) and activity_before (<=)."""
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get(
        "/api/sessions?activity_after=2026-06-01T00:00:00Z&activity_before=2026-06-30T23:59:59Z"
    )
    assert resp.status_code == 200
    count_query = str(mock_db.execute.call_args_list[0].args[0]).lower()
    assert ">=" in count_query
    assert "<=" in count_query


@pytest.mark.asyncio
async def test_list_sessions_excludes_sessions_without_messages(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get("/api/sessions")
    assert resp.status_code == 200

    # Message-less sessions (created on chat open / MCP setup) must be filtered
    # at the query level so pagination and totals stay honest.
    list_query = str(mock_db.execute.call_args_list[1].args[0])
    assert "EXISTS" in list_query
    assert "messages" in list_query


@pytest.mark.asyncio
async def test_list_sessions_search_is_word_independent(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    list_result = MagicMock()
    list_result.all.return_value = [(mock_session_obj, None)]
    share_agg = MagicMock()
    share_agg.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result, share_agg, share_agg])

    resp = await authenticated_client.get("/api/sessions?search=pod+crash")
    assert resp.status_code == 200

    # Two tokens × (title OR summary) = 4 escaped ILIKE predicates, AND-ed.
    list_query = str(mock_db.execute.call_args_list[1].args[0])
    assert list_query.upper().count("LIKE") >= 4
    assert "ESCAPE" in list_query.upper()


@pytest.mark.asyncio
async def test_list_sessions_whitespace_search_matches_nothing(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    # Whitespace-only search yields no tokens: a constant-false predicate is added so
    # it matches nothing (mirrors global search) — not an empty and_(), not "all".
    resp = await authenticated_client.get("/api/sessions?search=%20%20")
    assert resp.status_code == 200
    list_query = str(mock_db.execute.call_args_list[1].args[0]).upper()
    assert "ESCAPE" not in list_query
    assert "WHERE FALSE" in list_query or " FALSE" in list_query


@pytest.mark.asyncio
async def test_create_session_cluster_not_found(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.post(
        "/api/sessions",
        json={"cluster_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_session_cluster_access_denied(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_cluster: MagicMock,
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)

    with patch(
        "app.api.routes.sessions.check_cluster_access",
        side_effect=HTTPException(status_code=403, detail="Forbidden"),
    ):
        resp = await authenticated_client.post(
            "/api/sessions",
            json={"cluster_id": str(mock_cluster.id)},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_session_success(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_cluster: MagicMock,
    mock_session_obj: MagicMock,
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_session_obj.id
        obj.cluster_id = mock_cluster.id
        obj.user_id = mock_session_obj.user_id
        obj.title = None
        obj.summary = None
        obj.is_archived = False
        obj.total_input_tokens = 0
        obj.total_output_tokens = 0
        obj.total_tool_calls = 0
        obj.total_iterations = 0
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)
        obj.cluster = mock_cluster

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    with (
        patch("app.api.routes.sessions.check_cluster_access", return_value=None),
        patch("app.api.routes.sessions.notify", return_value=None),
    ):
        resp = await authenticated_client.post(
            "/api/sessions",
            json={"cluster_id": str(mock_cluster.id)},
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_session_without_cluster(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """a session can be created with no cluster configured."""
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_session_obj.id
        obj.cluster_id = None
        obj.user_id = mock_session_obj.user_id
        obj.title = None
        obj.summary = None
        obj.is_archived = False
        obj.total_input_tokens = 0
        obj.total_output_tokens = 0
        obj.total_tool_calls = 0
        obj.total_iterations = 0
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)
        obj.cluster = None

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    # No cluster lookup nor access check should run when cluster_id is absent.
    with (
        patch(
            "app.api.routes.sessions.check_cluster_access",
            side_effect=AssertionError("cluster access must not be checked"),
        ),
        patch("app.api.routes.sessions.notify", return_value=None),
    ):
        resp = await authenticated_client.post("/api/sessions", json={})
    assert resp.status_code == 201
    assert resp.json()["cluster_id"] is None
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_sessions_assigns_positions_by_request_order(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_user: MagicMock,
) -> None:
    s1, s2, s3 = MagicMock(), MagicMock(), MagicMock()
    s1.id, s2.id, s3.id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for s in (s1, s2, s3):
        s.user_id = str(mock_user.id)

    owned_result = MagicMock()
    owned_result.scalars.return_value.all.return_value = [s1, s2, s3]
    shares_result = MagicMock()
    shares_result.scalars.return_value.all.return_value = []  # no shared-in sessions
    # reorder issues two selects: owned sessions, then the caller's accepted shares.
    mock_db.execute = AsyncMock(side_effect=[owned_result, shares_result])
    mock_db.commit = AsyncMock()

    # Request a new top-to-bottom order: s3, s1, s2.
    with patch("app.api.routes.sessions.notify", return_value=None) as mock_notify:
        resp = await authenticated_client.post(
            "/api/sessions/reorder",
            json={"session_ids": [str(s3.id), str(s1.id), str(s2.id)]},
        )

    assert resp.status_code == 204
    assert (s3.position, s1.position, s2.position) == (0, 1, 2)
    mock_db.commit.assert_awaited_once()
    # Other tabs are notified (scoped to this user) so they re-fetch the new order.
    mock_notify.assert_awaited_once_with({"type": "session_reordered"}, user_id=str(mock_user.id))


@pytest.mark.asyncio
async def test_reorder_sessions_ignores_unowned_ids(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_user: MagicMock,
) -> None:
    owned = MagicMock()
    owned.id = uuid.uuid4()
    owned.user_id = str(mock_user.id)

    # The query only returns the caller's own sessions; a foreign id is filtered out.
    owned_result = MagicMock()
    owned_result.scalars.return_value.all.return_value = [owned]
    shares_result = MagicMock()
    shares_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[owned_result, shares_result])
    mock_db.commit = AsyncMock()

    foreign_id = uuid.uuid4()
    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.post(
            "/api/sessions/reorder",
            json={"session_ids": [str(foreign_id), str(owned.id)]},
        )

    assert resp.status_code == 204
    # Owned session takes position 0 (the foreign id at index 0 is skipped).
    assert owned.position == 0


@pytest.mark.asyncio
async def test_reorder_sessions_positions_shared_in_via_session_share(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_user: MagicMock,
) -> None:
    """A shared-with-me session is repositioned on the caller's own SessionShare row,
    interleaved with owned sessions in one contiguous 0..n sequence."""
    owned = MagicMock()
    owned.id = uuid.uuid4()
    owned.user_id = str(mock_user.id)
    share = MagicMock()  # the caller's accepted share of someone else's session
    share.session_id = uuid.uuid4()

    owned_result = MagicMock()
    owned_result.scalars.return_value.all.return_value = [owned]
    shares_result = MagicMock()
    shares_result.scalars.return_value.all.return_value = [share]
    mock_db.execute = AsyncMock(side_effect=[owned_result, shares_result])
    mock_db.commit = AsyncMock()

    # Put the shared-in session on top, owned below it.
    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.post(
            "/api/sessions/reorder",
            json={"session_ids": [str(share.session_id), str(owned.id)]},
        )

    assert resp.status_code == 204
    assert share.position == 0  # persisted on the participant's SessionShare row
    assert owned.position == 1


@pytest.mark.asyncio
async def test_get_session_not_found(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    sid = uuid.uuid4()
    resp = await authenticated_client.get(f"/api/sessions/{sid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_session_access_denied(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """User cannot access another user's session."""
    mock_session_obj.user_id = str(uuid.uuid4())  # different user
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_session(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.patch(
            f"/api/sessions/{mock_session_obj.id}",
            json={"title": "Updated Title"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_session(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    result_mock.scalars.return_value.all.return_value = []  # no participants
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.delete(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_session_notifies_shared_participants(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    """Deleting a shared conversation must notify the owner AND every accepted
    participant, so it drops from all their sidebars live — not just the owner's."""
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    result_mock.scalars.return_value.all.return_value = ["bob-id", "carol-id"]
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    notify_mock = AsyncMock()
    with patch("app.api.routes.sessions.notify", notify_mock):
        resp = await authenticated_client.delete(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 204

    notified = {call.kwargs["user_id"] for call in notify_mock.call_args_list}
    assert notified == {str(mock_user.id), "bob-id", "carol-id"}
    assert all(
        c.args[0] == {"type": "session_deleted", "session_id": str(mock_session_obj.id)}
        for c in notify_mock.call_args_list
    )


@pytest.mark.asyncio
async def test_delete_session_cancels_refresh_before_cleanup(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    """Session end must cancel any in-flight EKS refresh BEFORE tearing down the tmpfs,
    else a refresh mid-publish recreates the dir after cleanup (orphaned tmpfs)."""
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    result_mock.scalars.return_value.all.return_value = []  # no participants
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    order: list[str] = []
    with (
        patch("app.api.routes.sessions.notify", return_value=None),
        patch(
            "app.services.connectors.store.get_credential_store",
            return_value=MagicMock(revoke_session=AsyncMock()),
        ),
        patch(
            "app.services.eks_refresh.cancel_session_refresh",
            AsyncMock(side_effect=lambda *_: order.append("cancel")),
        ),
        patch(
            "app.tools.connector_exec.cleanup_aws_files",
            side_effect=lambda *a, **k: order.append("clear"),
        ),
        patch(
            "app.services.eks_refresh.release_session_lock",
            side_effect=lambda *_: order.append("release"),
        ),
    ):
        resp = await authenticated_client.delete(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 204
    assert order == ["cancel", "clear", "release"]  # cancel strictly before teardown


@pytest.mark.asyncio
async def test_get_session_stats(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_input_tokens"] == 100
    assert data["total_output_tokens"] == 50


@pytest.mark.asyncio
async def test_get_failed_messages_empty(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    msg_result = MagicMock()
    msg_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[session_result, msg_result])

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/failed-messages")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_search_requires_min_length(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/search?q=x")
    assert resp.status_code == 422


# ============================================================================
# Word-independent search helpers
# ============================================================================


def test_escape_like_neutralizes_metacharacters() -> None:
    from app.api.routes.sessions import _escape_like

    # %, _ and backslash must be escaped so they match literally, not as wildcards.
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a_b") == "a\\_b"
    assert _escape_like("c:\\path") == "c:\\\\path"


def test_search_tokens_splits_and_dedups_case_insensitively() -> None:
    from app.api.routes.sessions import _search_tokens

    # Whitespace-split, case-insensitive dedup, first-seen casing kept.
    assert _search_tokens("pod   crash Pod") == ["pod", "crash"]
    assert _search_tokens("") == []
    assert _search_tokens("   ") == []


def test_search_tokens_caps_count() -> None:
    from app.api.routes.sessions import MAX_SEARCH_TOKENS, _search_tokens

    many = " ".join(f"t{i}" for i in range(MAX_SEARCH_TOKENS + 5))
    assert len(_search_tokens(many)) == MAX_SEARCH_TOKENS


def test_make_snippet_anchors_on_earliest_token() -> None:
    from app.api.routes.sessions import SNIPPET_LENGTH, _make_snippet

    text = "x" * 200 + " crash " + "y" * 50 + " pod " + "z" * 200
    # "pod" appears before "crash" in the token list but later in the text;
    # the snippet anchors on whichever token occurs first in the text ("crash").
    snippet = _make_snippet(text, ["pod", "crash"])
    assert "crash" in snippet
    assert len(snippet) <= SNIPPET_LENGTH + 2  # +2 for the ellipsis chars


def test_make_snippet_falls_back_to_head_when_no_token_matches() -> None:
    from app.api.routes.sessions import SNIPPET_LENGTH, _make_snippet

    text = "a" * 300
    snippet = _make_snippet(text, ["nope"])
    assert snippet == text[:SNIPPET_LENGTH]


@pytest.mark.asyncio
async def test_search_whitespace_only_short_circuits(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # q="  " passes the min_length=2 validator but yields no tokens: must return
    # empty WITHOUT hitting the DB (guards and_() with no clauses).
    mock_db.execute = AsyncMock()

    resp = await authenticated_client.get("/api/search?q=%20%20")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []
    assert data["total"] == 0
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_search_multi_token_matches_any_order(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    # Session title has the words in the opposite order to the query; the DB does
    # the ILIKE matching, so here we assert the endpoint wires a title hit through
    # and computes title_match from the (word-independent) tokens.
    mock_session_obj.user_id = str(mock_user.id)
    mock_session_obj.title = "crash loop on the pod"
    mock_session_obj.summary = None

    title_result = MagicMock()
    title_result.scalars.return_value.all.return_value = [mock_session_obj]
    msg_result = MagicMock()
    msg_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[title_result, msg_result])

    resp = await authenticated_client.get("/api/search?q=pod%20crash")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    hit = data["results"][0]
    assert hit["title_match"] is True
    assert hit["matched_messages"] == []


# ============================================================================
# Per-session model selection
# ============================================================================


@pytest.mark.asyncio
async def test_create_session_rejects_disabled_model(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await authenticated_client.post(
        "/api/sessions",
        json={"model": "gpt-4o"},
    )
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_session_rejects_disabled_model(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.patch(
        f"/api/sessions/{mock_session_obj.id}",
        json={"model": "gpt-4o"},
    )
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_session_accepts_enabled_model(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.patch(
            f"/api/sessions/{mock_session_obj.id}",
            json={"model": "claude-opus-4-20250514"},
        )
    assert resp.status_code == 200
    assert mock_session_obj.model == "claude-opus-4-20250514"


@pytest.mark.asyncio
async def test_update_session_unpins_model_with_null(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    """Explicit null clears the pin so the session follows the global default."""
    mock_session_obj.user_id = str(mock_user.id)
    mock_session_obj.model = "claude-opus-4-20250514"
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.patch(
            f"/api/sessions/{mock_session_obj.id}",
            json={"model": None},
        )
    assert resp.status_code == 200
    assert mock_session_obj.model is None


@pytest.mark.asyncio
async def test_update_session_rejects_disabled_engine(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.patch(
        f"/api/sessions/{mock_session_obj.id}",
        json={"engine": "bogus-engine"},
    )
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_session_accepts_engine_pin_for_claude(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    # a Claude model can be pinned to the inkwest-agent engine (Converse).
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.patch(
            f"/api/sessions/{mock_session_obj.id}",
            json={"model": "claude-opus-4-20250514", "engine": "inkwest-agent"},
        )
    assert resp.status_code == 200
    assert mock_session_obj.engine == "inkwest-agent"
