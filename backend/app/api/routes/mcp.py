"""
MCP catalog routes.

Exposes the *keys* of the MCP servers users may enable per conversation. Server
definitions (and any secret they embed) stay server-side — only keys leave the
backend. Per-session selection lives on the session resource (see sessions.py).
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import CurrentUser
from app.services.mcp_catalog import catalog_keys

router = APIRouter(prefix="/mcp", tags=["mcp"])


class McpCatalogResponse(BaseModel):
    """Available MCP server keys (no definitions, no secrets)."""

    keys: list[str]


@router.get("/catalog", response_model=McpCatalogResponse)
async def get_catalog(current_user: CurrentUser) -> McpCatalogResponse:
    """List the MCP server keys available for selection."""
    return McpCatalogResponse(keys=catalog_keys())
