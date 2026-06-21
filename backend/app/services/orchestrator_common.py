"""
Turn finalization shared by the two API-based, non-streaming chat orchestrators:
``orchestrator.orchestrate_chat`` (Anthropic SDK) and ``orchestrator_converse``
(Bedrock Converse).

Persisting per-turn usage/session stats is identical for both, so it lives here —
the fix lands once. This half is DB-bound (sqlalchemy); the DB-free tool-call loop
the same orchestrators (and the standalone ``inkwest_agent``) share was carved out
to ``tool_execution.py``.

Out of scope by design: ``orchestrator_cli`` drives an external CLI that runs its
own tool loop, so it has nothing to share here.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.budget_service import record_usage
from app.services.messages import update_session_stats

logger = logging.getLogger(__name__)


async def finalize_turn(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID | None,
    model: str,
    total_input_tokens: int,
    total_output_tokens: int,
    total_tool_calls: int,
    iteration: int,
    iter_latency_ms: int,
) -> dict[str, Any]:
    """Persist session stats + usage and return the done-event metadata.

    Persistence is best-effort: on failure the turn still completes, with
    ``usage_tracking_failed`` flagged in the metadata for the client.
    """
    usage_failed = False
    try:
        await update_session_stats(
            db,
            session_id,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            tool_calls=total_tool_calls,
            iterations=iteration,
        )
        await record_usage(
            db,
            user_id=user_id,
            session_id=session_id,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            model=model,
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to persist session stats/usage: {e}")
        usage_failed = True
        # Leave the session usable: a failed commit aborts the transaction, so
        # roll back before the turn ends (defensive — the per-turn session is
        # normally discarded right after).
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback after usage-persist failure also failed")

    meta: dict[str, Any] = {
        "iterations": iteration,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "tool_calls": total_tool_calls,
        "latency_ms": iter_latency_ms,
    }
    if usage_failed:
        meta["usage_tracking_failed"] = True
    return meta
