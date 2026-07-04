"""
Shared end-of-turn usage persistence for the three engine orchestrators.

One implementation of the sequence every orchestrator needs at turn end (success, error,
timeout AND user-stop paths): session-stats increment → engine-side budget row (skipped
when the broker already metered the turn) → commit, then the display-only Message stamp
contained in its own transaction. Previously triplicated as near-identical closures in
the cli/inkwest-agent/opencode orchestrators — which had already drifted (the CLI copy
lost the rollback its siblings documented adding "for parity").

Contracts:
- **Gate**: nothing persists when every count is zero — but cache counts join the gate
  (a fully-cached turn reports 0 fresh in/out yet burned billable tokens).
- **Broker authority**: ``skip_budget_row=True`` (grant minted ⇒ broker metering active)
  skips ONLY the ``record_usage`` row; session stats and the Message stamp stay
  engine-fed (they feed the UI, not the budget).
- **Best-effort + one retry**: a transient DB blip at turn end must not silently lose
  the turn's only accounting row (broker-off), so the stats+budget block retries once
  after a short pause; failures roll the session back (aborted-transaction hygiene) and
  are logged, never raised — persistence must not mask the turn's own outcome. The
  retry first checks whether the turn's budget row already landed (an ambiguous
  commit — succeeded server-side, ack lost — must not double-apply).
- **Stamp isolation**: the per-bubble token stamp runs AFTER the budget row in its own
  try — its failure can never cost the turn its authoritative usage record.
"""

import asyncio
import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import UsageRecord
from app.services.budget_service import record_usage
from app.services.messages import stamp_message_usage, update_session_stats

logger = logging.getLogger(__name__)

# One immediate-ish retry covers the transient-blip class (connection reset, brief
# failover) without turning turn-end into a retry loop that delays the caller.
_RETRY_DELAY_SECONDS = 0.5


async def _engine_row_exists(db: AsyncSession, turn_id: uuid.UUID) -> bool:
    """Did the turn's engine-side budget row already commit? False on any lookup error
    (a still-down DB must fall through to the normal retry, not mask it)."""
    try:
        row = await db.scalar(
            select(UsageRecord.id)
            .where(UsageRecord.turn_id == turn_id, UsageRecord.source == "engine")
            .limit(1)
        )
        return row is not None
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return False


async def persist_turn_usage(
    db: AsyncSession,
    *,
    session_id: str | uuid.UUID,
    session_uuid: uuid.UUID,
    user_id: uuid.UUID | None,
    model: str | None,
    turn_id: uuid.UUID,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    tool_calls: int = 0,
    iterations: int = 1,
    skip_budget_row: bool = False,
    cost_usd: Decimal | None = None,
    stamp_message_id: uuid.UUID | None = None,
) -> None:
    """Persist one turn's accumulated usage (stats + budget row + Message stamp)."""
    if not (input_tokens or output_tokens or cache_read_tokens or cache_write_tokens):
        return

    for attempt in (1, 2):
        try:
            # Commit-ambiguity guard: attempt 1's COMMIT can succeed server-side with the
            # ack lost (connection reset at commit — the very blip this retry covers).
            # Re-applying would double the session stats and insert a second budget row,
            # silently inflating spend. The budget row landing proves the whole txn did
            # (stats + row commit together). If this probe races a still-healing
            # connection and misses the row, the DB backstop catches it: the unique
            # partial index uq_usage_records_engine_turn rejects the second engine row
            # and rolls the whole retry txn back. Broker-on turns write no engine row,
            # so their retry has no marker — residual double-apply there touches display
            # stats only, never the budget (broker rows are the accounting authority).
            if attempt == 2 and not skip_budget_row and await _engine_row_exists(db, turn_id):
                logger.info("Turn usage already committed (ambiguous first commit); not retrying")
                break
            await update_session_stats(
                db,
                session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_calls=tool_calls,
                iterations=iterations,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )
            # Broker-on: the proxy already persisted one authoritative UsageRecord per
            # Bedrock call (source='broker', wire-exact cache tokens) — an engine-side
            # row would double-count the budget.
            if not skip_budget_row:
                await record_usage(
                    db,
                    user_id=user_id,
                    session_id=session_uuid,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model=model,
                    cost_usd=cost_usd,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    cache_write_1h_tokens=cache_write_1h_tokens,
                    turn_id=turn_id,
                )
            await db.commit()
            break
        except Exception as e:
            logger.error(f"Failed to persist session stats/usage (attempt {attempt}/2): {e}")
            # A failed commit leaves the session in an aborted transaction; roll back so
            # the retry (and any later DB op on this session) doesn't error.
            try:
                await db.rollback()
            except Exception:
                pass
            if attempt == 1:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)

    # Display-only stamp, contained in its OWN try AFTER the budget row: a stamp failure
    # must never cost the turn its authoritative usage record.
    if stamp_message_id is not None:
        try:
            await stamp_message_usage(
                db,
                stamp_message_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                session_id=session_uuid,
            )
        except Exception as e:
            logger.warning(f"Failed to stamp message usage: {e}")
            try:
                await db.rollback()
            except Exception:
                pass
