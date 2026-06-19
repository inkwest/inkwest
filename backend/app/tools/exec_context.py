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
    # Set by the sandboxed inkwest-agent: connector creds were *pre-provided* by the
    # broker (projected to a tmpfs + injected as env), so the executors source them
    # from the environment, NOT the portal credential store — which the agent process
    # can't reach and must never call back to (one-way handoff). The portal
    # orchestrators leave this False and use the in-RAM store + renew-on-read.
    projected_creds: bool = False
