"""
Pydantic schemas for API request/response validation.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
# Prompt Template Schemas
# =============================================================================


class PromptTemplateBase(BaseModel):
    """Base prompt template schema with common fields."""

    name: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1)
    description: str | None = None


class PromptTemplateCreate(PromptTemplateBase):
    """Schema for creating a new prompt template."""

    pass


class PromptTemplateUpdate(BaseModel):
    """Schema for updating a prompt template."""

    name: str | None = Field(None, min_length=1, max_length=255)
    content: str | None = Field(None, min_length=1)
    description: str | None = None
    is_active: bool | None = None


class PromptTemplateResponse(PromptTemplateBase):
    """Schema for prompt template response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PromptTemplateList(BaseModel):
    """Schema for list of prompt templates."""

    items: list[PromptTemplateResponse]
    total: int


# =============================================================================
# Message Schemas
# =============================================================================


class MessageBase(BaseModel):
    """Base message schema with common fields."""

    role: str = Field(..., pattern="^(user|assistant|tool_result)$")
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
    token_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
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
    """Schema for creating a new session."""

    pass


class SessionUpdate(BaseModel):
    """Schema for updating a session."""

    title: str | None = Field(None, max_length=255)
    summary: str | None = None
    is_archived: bool | None = None


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
    cluster: ClusterSummary | None = None
    # Usage statistics
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tool_calls: int = 0
    total_iterations: int = 0
    # Timestamps
    created_at: datetime
    updated_at: datetime


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
