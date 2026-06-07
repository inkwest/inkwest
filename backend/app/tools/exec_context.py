"""
Tool execution context.

Carries the identity (user + session) and cluster targeting needed to execute a
tool. For connector tools the ``user_id``/``session_id`` are how the executor
looks up the minted credential in the RAM store — the credential is never threaded
through the model or the tool input.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ToolExecContext:
    user_id: UUID | None
    session_id: UUID
    kubeconfig_path: str | None = None
    context_name: str | None = None
