"""Tests for the per-user fan-out in the pub/sub layer."""

import pytest

from app.services import pubsub


@pytest.mark.asyncio
async def test_fan_out_is_scoped_to_the_event_user() -> None:
    # A scoped subscriber only receives events for its own user; an unscoped one
    # (None, e.g. dev single-user) receives everything.
    async with (
        pubsub.subscribe("alice") as q_alice,
        pubsub.subscribe("bob") as q_bob,
        pubsub.subscribe(None) as q_all,
    ):
        pubsub._fan_out({"type": "session_updated", "user_id": "alice"})

        assert q_alice.get_nowait()["user_id"] == "alice"
        assert q_all.get_nowait()["user_id"] == "alice"
        assert q_bob.empty()  # bob never sees alice's activity


@pytest.mark.asyncio
async def test_fan_out_without_user_id_broadcasts() -> None:
    # An event missing user_id degrades to the old broadcast (never dropped).
    async with pubsub.subscribe("alice") as q_alice, pubsub.subscribe("bob") as q_bob:
        pubsub._fan_out({"type": "session_updated"})

        assert not q_alice.empty()
        assert not q_bob.empty()
