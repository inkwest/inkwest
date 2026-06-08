"""
System prompt templates for Inkwest AI assistant.

Contains the system prompt that defines the AI's behavior,
capabilities, and guidelines for Kubernetes debugging.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uuid  # noqa: F401  (used in string annotations)

    from sqlalchemy.ext.asyncio import AsyncSession

SYSTEM_PROMPT = """You are Inkwest, an AI assistant specialized in debugging Kubernetes clusters.

## Your Role
You help users diagnose and understand issues in their Kubernetes clusters. \
You operate in **read-only mode** — you can only observe and gather information, \
never modify cluster state.

## Read-Only Constraint
- You can ONLY run read-only kubectl commands \
(get, describe, logs, events, top, explain, api-resources).
- You CANNOT create, update, delete, apply, patch, scale, run, \
exec, or modify any cluster resource.
- When recommending fixes, present them as suggestions \
for the user to review and execute themselves.
- Always warn about potential impact when suggesting \
destructive or state-changing commands.

## Current Context
- Cluster: {cluster_name}
- Available namespaces: {namespaces}

## Available Tools
You can use these read-only kubectl tools to investigate:
- `kubectl_get` - List or retrieve resources
- `kubectl_describe` - Get detailed resource information
- `kubectl_logs` - Get pod logs
- `kubectl_events` - Get cluster events
- `kubectl_top` - Check resource usage
- `kubectl_explain` - Explain resource schemas
- `kubectl_api_resources` - List available API resources

## Guidelines

1. **Be systematic**: Start broad (list resources), then narrow down \
(describe specific resources, get logs)

2. **Explain your reasoning**: Tell the user what you're checking and why

3. **Use multiple tools**: Don't hesitate to run several commands to gather complete information

4. **Check events**: Events often reveal the root cause of issues

5. **Look at logs**: For application issues, always check pod logs

6. **Consider common causes**:
   - Image pull errors (wrong image, no pull secret)
   - Resource limits (OOMKilled, CPU throttling)
   - Probe failures (readiness/liveness)
   - Configuration errors (missing ConfigMaps, Secrets)
   - Networking (service selectors, network policies)
   - Storage (PVC pending, mount failures)

7. **Provide actionable advice**: After diagnosis, suggest specific fixes \
as commands or manifests the user can review and run themselves

8. **Be concise**: Summarize findings, don't dump raw output unless asked

## Response Format

Structure your final response using these sections \
(use only the ones relevant to the investigation):

### Investigation
Brief description of what was checked and why.

### Findings
Bullet-point list of key observations from the collected data. \
Flag items as `[OK]`, `[WARN]`, or `[ERROR]` where appropriate.

### Root Cause
Clear statement of the underlying issue (if identified). \
If uncertain, list the most likely causes ranked by probability.

### Recommendations
Numbered, actionable steps to resolve the issue. \
Include example commands or manifest snippets when helpful.

Keep it concise — do not dump raw tool output unless the user asks. \
Use markdown formatting for readability.
"""


def get_system_prompt(
    cluster_name: str | None,
    namespaces: list[str] | None = None,
    *,
    base_prompt: str | None = None,
) -> str:
    """
    Generate system prompt with cluster context.

    Args:
        cluster_name: Name of the Kubernetes cluster, or None when the session
            has no Kube context configured
        namespaces: List of available namespaces (optional)
        base_prompt: The prompt body to use (DB default / admin-selected /
            custom). Falls back to the hardcoded ``SYSTEM_PROMPT`` when None.

    Returns:
        Formatted system prompt string
    """
    from app.core.config import get_settings

    if cluster_name is None:
        cluster_label = (
            "none configured — no Kubernetes access in this session. "
            "kubectl tools will return an error until a cluster or AWS/EKS access is set up."
        )
    else:
        cluster_label = cluster_name
    ns_str = ", ".join(namespaces) if namespaces else "all (user will specify)"

    template = base_prompt if base_prompt is not None else SYSTEM_PROMPT
    # Literal token replacement (not str.format) so admin-supplied custom prompts
    # containing stray braces never raise. The two blanks are optional.
    prompt = template.replace("{cluster_name}", cluster_label).replace("{namespaces}", ns_str)

    extra = get_settings().system_prompt_extra
    if extra:
        prompt += f"\n\n## Additional Instructions\n{extra}\n"

    return prompt


DEFAULT_SYSTEM_PROMPT_NAME = "Default"


async def ensure_default_system_prompt(db: "AsyncSession") -> None:
    """Seed the hardcoded prompt as the DB default on first startup (idempotent).

    Only inserts when the table is empty, so admin edits/renames are preserved
    and a default always exists out of the box.
    """
    from sqlalchemy import func, select

    from app.models.system_prompt import SystemPrompt

    count = await db.scalar(select(func.count()).select_from(SystemPrompt))
    if count:
        return

    db.add(
        SystemPrompt(
            name=DEFAULT_SYSTEM_PROMPT_NAME,
            content=SYSTEM_PROMPT,
            description="Built-in default Inkwest system prompt (seeded from code).",
            is_active=True,
            is_default=True,
        )
    )
    await db.commit()


async def resolve_session_system_prompt(
    db: "AsyncSession", session_id: "str | uuid.UUID"
) -> str | None:
    """Resolve the base system prompt body for a session.

    Precedence: session custom text → session-selected prompt → global default.
    Returns None when nothing is found, so ``get_system_prompt`` falls back to
    the hardcoded ``SYSTEM_PROMPT``.
    """
    from sqlalchemy import select

    from app.models.session import Session
    from app.models.system_prompt import SystemPrompt

    session = await db.get(Session, session_id)
    if session is not None:
        if session.system_prompt_custom:
            return session.system_prompt_custom
        if session.system_prompt_id is not None:
            selected = await db.get(SystemPrompt, session.system_prompt_id)
            if selected is not None and selected.is_active:
                return selected.content

    default = await db.scalar(
        select(SystemPrompt).where(SystemPrompt.is_default == True)  # noqa: E712
    )
    return default.content if default is not None else None


# Additional prompt templates for specific scenarios

SUMMARY_PROMPT = """Based on the conversation so far, provide a brief summary (2-3 sentences) of:
1. What the user was investigating
2. What was found
3. Any actions recommended

Be concise and focus on key findings."""


TITLE_GENERATION_PROMPT = (
    "Generate a short, descriptive title (max 60 characters)"
    " for this conversation based on the first user message:\n\n"
    '"{message}"\n\n'
    "The title should capture the main topic or issue being"
    " discussed. Respond with just the title, nothing else."
)


def get_summary_prompt() -> str:
    """Get the prompt for generating conversation summaries."""
    return SUMMARY_PROMPT


def get_title_prompt(message: str) -> str:
    """
    Get the prompt for generating session titles.

    Args:
        message: The first user message

    Returns:
        Formatted title generation prompt
    """
    return TITLE_GENERATION_PROMPT.format(message=message[:200])
