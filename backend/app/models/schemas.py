"""
Pydantic schemas for API request/response validation.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.core.skill_md import frontmatter_argument_hint
from app.core.slug import slugify

# =============================================================================
# Cluster Schemas
# =============================================================================


class ClusterBase(BaseModel):
    """Base cluster schema with common fields."""

    name: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    kubeconfig_path: str = Field(..., min_length=1, max_length=1024)
    context_name: str | None = Field(None, max_length=255)
    description: str | None = None


class ClusterCreate(ClusterBase):
    """Schema for creating a new cluster."""

    pass


class ClusterUpdate(BaseModel):
    """Schema for updating a cluster."""

    display_name: str | None = Field(None, min_length=1, max_length=255)
    kubeconfig_path: str | None = Field(None, min_length=1, max_length=1024)
    context_name: str | None = Field(None, max_length=255)
    description: str | None = None
    is_active: bool | None = None


class ClusterResponse(ClusterBase):
    """Schema for cluster response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ClusterList(BaseModel):
    """Schema for list of clusters."""

    items: list[ClusterResponse]
    total: int


# =============================================================================
# Skill Schemas
# =============================================================================


class SkillCreate(BaseModel):
    """Create a skill from a single SKILL.md document (frontmatter + body).

    ``name``/``description``/``arguments`` are parsed from the frontmatter, not sent
    as separate fields (see app.core.skill_md).
    """

    content: str = Field(..., min_length=1)


class SkillUpdate(BaseModel):
    """Update a skill. ``content`` is the full SKILL.md; ``is_active`` toggles only."""

    content: str | None = Field(None, min_length=1)
    is_active: bool | None = None


class SkillResponse(BaseModel):
    """Schema for skill response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    content: str
    description: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]  # mypy: decorator over @property
    @property
    def slug(self) -> str:
        """The ``/<slug>`` skill trigger. ``name`` is already the normalized slug
        (set on save); slugify again defensively."""
        return slugify(self.name)

    @computed_field  # type: ignore[prop-decorator]  # mypy: decorator over @property
    @property
    def argument_hint(self) -> str | None:
        """Argument hint parsed from the SKILL.md frontmatter (``arguments`` /
        ``argument-hint``); None if absent."""
        return frontmatter_argument_hint(self.content)


class SkillList(BaseModel):
    """Schema for list of skills."""

    items: list[SkillResponse]
    total: int


# =============================================================================
# User Preferences & Personal Skills Schemas
# =============================================================================


class UserSkillCreate(BaseModel):
    """Create a personal skill from a single SKILL.md document (frontmatter + body).

    Same shape as ``SkillCreate`` — name/description/arguments come from the frontmatter
    so the admin CRUD UI is reused verbatim for personal skills.
    """

    content: str = Field(..., min_length=1)


class UserSkillUpdate(BaseModel):
    """Update a personal skill. ``content`` is the full SKILL.md; ``is_active`` toggles."""

    content: str | None = Field(None, min_length=1)
    is_active: bool | None = None


class UserSkillResponse(BaseModel):
    """A personal skill. Mirrors ``SkillResponse`` (so the shared CRUD component renders
    it) plus the override provenance fields."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    content: str
    description: str | None = None
    is_active: bool
    # True when this personal skill shadows an admin skill of the same name (badge).
    is_override: bool = False
    # The admin skill this was forked from, when known (provenance only).
    source_admin_skill_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def slug(self) -> str:
        """The ``/<slug>`` trigger. ``name`` is already the normalized slug."""
        return slugify(self.name)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def argument_hint(self) -> str | None:
        """Argument hint parsed from the SKILL.md frontmatter; None if absent."""
        return frontmatter_argument_hint(self.content)


class PreferencesUpdate(BaseModel):
    """Replace the preferences document (UI settings + ``skills.disabled_admin``).

    The document is opaque to the backend except for ``skills.disabled_admin``; the
    route enforces only a size quota, not a deep schema.
    """

    document: dict[str, Any] = Field(default_factory=dict)


class PreferencesResponse(BaseModel):
    """The current user's full preferences: the versioned document plus their personal
    skills (one round-trip for the Settings modal)."""

    schema_version: int
    document: dict[str, Any]
    skills: list[UserSkillResponse]


class ExportSkill(BaseModel):
    """A personal skill as it appears in an export envelope."""

    name: str
    content: str
    is_active: bool
    is_override: bool


class ConfigExport(BaseModel):
    """The downloadable, versioned config: preferences document + personal skills.

    Re-importable verbatim (POST /api/me/import) — ``replace_all`` is a request flag, not
    part of the envelope, so a saved export round-trips without edits.
    """

    schema_version: int
    exported_at: datetime
    preferences: dict[str, Any]
    skills: list[ExportSkill]


class ImportSkill(BaseModel):
    """One skill in an import payload. Only ``content`` (+ optional ``is_active``) is
    honored: name/description/is_override are re-derived server-side. Extra keys from an
    export envelope (e.g. ``name``) are ignored, so an export imports unedited."""

    model_config = ConfigDict(extra="ignore")

    content: str = Field(..., min_length=1)
    is_active: bool = True


class ConfigImport(BaseModel):
    """An import payload — the same shape an export produces (extra keys ignored)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int
    preferences: dict[str, Any] = Field(default_factory=dict)
    skills: list[ImportSkill] = Field(default_factory=list)


# =============================================================================
# System Prompt Schemas
# =============================================================================


class SystemPromptBase(BaseModel):
    """Base system prompt schema with common fields."""

    name: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1)
    description: str | None = None


class SystemPromptCreate(SystemPromptBase):
    """Schema for creating a new system prompt."""

    is_default: bool = False


class SystemPromptUpdate(BaseModel):
    """Schema for updating a system prompt."""

    name: str | None = Field(None, min_length=1, max_length=255)
    content: str | None = Field(None, min_length=1)
    description: str | None = None
    is_active: bool | None = None
    is_default: bool | None = None


class SystemPromptResponse(SystemPromptBase):
    """Schema for system prompt response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class SystemPromptList(BaseModel):
    """Schema for list of system prompts."""

    items: list[SystemPromptResponse]
    total: int


# =============================================================================
# Message Schemas
# =============================================================================


class MessageBase(BaseModel):
    """Base message schema with common fields."""

    role: str = Field(..., pattern="^(user|assistant|tool_result|system)$")
    content: dict[str, Any] | str | list[Any]  # Support dict, string, or list content
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class MessageCreate(MessageBase):
    """Schema for creating a new message."""

    pass


class MessageResponse(MessageBase):
    """Schema for message response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    # Who sent this user turn in a shared conversation; NULL = the AI or pre-sharing.
    author_user_id: UUID | None = None
    # Files attached to a user turn (descriptor dicts); NULL/absent otherwise.
    attachments: list[dict[str, Any]] | None = None
    # Model that produced this assistant turn (NULL for older/non-assistant rows).
    model: str | None = None
    # Agentic engine that produced this assistant turn (NULL for older/non-assistant rows).
    engine: str | None = None
    token_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Prompt-cache splits for the turn (per-bubble token line); NULL for older rows.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    thinking_content: str | None = None
    latency_ms: int | None = None
    status: str = "success"  # pending, success, error
    error_message: str | None = None
    is_retryable: bool = False
    retry_count: int = 0
    created_at: datetime


# =============================================================================
# Session Schemas
# =============================================================================


class SessionBase(BaseModel):
    """Base session schema with common fields."""

    cluster_id: UUID | None = None
    title: str | None = Field(None, max_length=255)


class SessionCreate(SessionBase):
    """Schema for creating a new session.

    ``system_prompt_id`` / ``system_prompt_custom`` are admin-only; non-admins
    must omit both and get the global default (enforced in the route).
    """

    system_prompt_id: UUID | None = None
    system_prompt_custom: str | None = Field(None, min_length=1)
    # One of the admin-enabled models (enforced in the route); omitted = default.
    model: str | None = Field(None, min_length=1, max_length=255)
    # Agentic engine pairing for the model (enforced in the route); omitted = resolve
    # per turn. Only meaningful when more than one engine is enabled (the picker).
    engine: str | None = Field(None, min_length=1, max_length=50)


class SessionUpdate(BaseModel):
    """Schema for updating a session."""

    title: str | None = Field(None, max_length=255)
    summary: str | None = None
    is_archived: bool | None = None
    # Changeable mid-session; validated against the enabled models in the route.
    model: str | None = Field(None, min_length=1, max_length=255)
    # Changeable mid-session; validated against the enabled engines in the route.
    engine: str | None = Field(None, min_length=1, max_length=50)


class SessionReorder(BaseModel):
    """Top-to-bottom ordered session ids defining the sidebar arrangement."""

    session_ids: list[UUID]


class ClusterSummary(BaseModel):
    """Minimal cluster info for embedding in session responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    display_name: str


class SessionResponse(SessionBase):
    """Schema for session response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None = None
    summary: str | None = None
    is_archived: bool
    # Collaboration markers, set by the route per caller (default = own session):
    #   is_owner           — caller owns it (or is admin); False = shared with them
    #   shared_role        — 'viewer' | 'editor' when shared-in; None when owned
    #   owner_display_name — the owner's name, for the "shared by X" hint
    is_owner: bool = True
    shared_role: str | None = None
    owner_display_name: str | None = None
    # How many people + active links this conversation is shared with (owner view only;
    # 0 when not shared or not the owner). Drives the "Shared·N" state on the Share control.
    share_count: int = 0
    cluster: ClusterSummary | None = None
    system_prompt_id: UUID | None = None
    system_prompt_custom: str | None = None
    model: str | None = None
    engine: str | None = None
    # Usage statistics
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_tool_calls: int = 0
    total_iterations: int = 0
    # Timestamps
    created_at: datetime
    updated_at: datetime
    # Newest message timestamp — drives the "last activity" sort. None when the session
    # has no messages yet. Populated by the list route, not by model_validate.
    last_message_at: datetime | None = None


class SessionWithMessages(SessionResponse):
    """Schema for session with messages."""

    messages: list[MessageResponse] = []


class SessionList(BaseModel):
    """Schema for list of sessions."""

    items: list[SessionResponse]
    total: int
    page: int
    page_size: int


# =============================================================================
# Sharing / Collaboration Schemas
# =============================================================================


class UserSummary(BaseModel):
    """Minimal user info for the share picker (no roles / OIDC subject)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    email: str


class ShareCreate(BaseModel):
    """Invite a user to a session. Shares are read-only (view-only); only the owner
    sends, so no role is accepted."""

    user_id: UUID


class ShareRespond(BaseModel):
    """Accept or decline a pending invitation."""

    accept: bool


class ParticipantResponse(BaseModel):
    """A session participant (or pending invitee), for the owner's share panel.

    Built from a SessionShare row joined to its user — not validated from
    attributes, since display_name/email come from the joined User.
    """

    id: UUID  # the share row id
    session_id: UUID
    user_id: UUID
    display_name: str
    email: str
    role: str
    status: str
    created_at: datetime
    responded_at: datetime | None = None


class PendingInviteResponse(BaseModel):
    """A pending invitation addressed to the current user (drives the popup)."""

    share_id: UUID
    session_id: UUID
    session_title: str | None = None
    role: str
    invited_by_display_name: str | None = None
    created_at: datetime


class ShareLinkCreate(BaseModel):
    """Mint a revokable anonymous read-only share link; optional human label."""

    label: str | None = Field(None, max_length=255)


class ShareLinkResponse(BaseModel):
    """A share link, as returned to the owner. ``url`` is the full shareable URL."""

    id: UUID
    token: str
    url: str
    label: str | None = None
    is_active: bool
    created_at: datetime


class SharedMessage(BaseModel):
    """A message on the public read-only share view — a deliberately narrow projection.

    Anonymous link visitors get only what the transcript shows (role + content +
    timestamp); internal fields carried by MessageResponse (thinking_content, token
    counts, model/engine, author_user_id, error/retry state) are intentionally NOT
    exposed on the unauthenticated endpoint.
    """

    id: UUID
    role: str
    content: dict[str, Any] | str | list[Any]
    created_at: datetime


class SharedConversationResponse(BaseModel):
    """Read-only conversation served to an anonymous link visitor."""

    session_id: UUID
    title: str | None = None
    owner_display_name: str | None = None
    messages: list[SharedMessage] = []


# =============================================================================
# Search Schemas
# =============================================================================


class SearchMessageHit(BaseModel):
    id: UUID
    role: str
    snippet: str
    created_at: datetime


class SearchSessionHit(BaseModel):
    id: UUID
    title: str | None
    cluster: ClusterSummary | None
    created_at: datetime
    updated_at: datetime
    matched_messages: list[SearchMessageHit]
    title_match: bool


class SearchResponse(BaseModel):
    query: str
    results: list[SearchSessionHit]
    total: int


# =============================================================================
# Health Schemas
# =============================================================================


class HealthResponse(BaseModel):
    """Schema for health check response."""

    status: str


class ReadyResponse(BaseModel):
    """Schema for readiness check response."""

    status: str
    database: str = "connected"


class VersionResponse(BaseModel):
    """Build metadata of the running backend, for client update detection."""

    version: str
    commit: str
    build_timestamp: str


# =============================================================================
# Error Schemas
# =============================================================================


class ErrorResponse(BaseModel):
    """Schema for error response."""

    detail: str
    code: str | None = None


class ValidationErrorResponse(BaseModel):
    """Schema for validation error response."""

    detail: list[dict[str, Any]]


# ---- MCP per-tool permission policy (shared by /api/me + /api/sessions) ------ //
class McpPolicyEntry(BaseModel):
    """One resolved policy row: a tool's verdict for a server (tool='*' = server default)."""

    server_key: str
    tool: str
    verdict: str  # allow | deny | ask


class McpPolicyResponse(BaseModel):
    """All policy rows in a scope (a user's globals, or a conversation's overrides)."""

    policies: list[McpPolicyEntry]


class McpToolVerdict(BaseModel):
    tool: str  # a tool name, or '*' for the server-wide default
    verdict: str  # allow | deny | ask


class McpPolicyServerUpdate(BaseModel):
    """Replace all of one server's tool verdicts in a scope (per-server full-state PUT)."""

    server_key: str
    entries: list[McpToolVerdict]
