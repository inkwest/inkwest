"""
Application-wide constants.

Centralizes magic numbers and configuration values that are used
across multiple modules.
"""

# =============================================================================
# AI Orchestration
# =============================================================================

# Maximum number of tool-use iterations before forcing stop
MAX_ORCHESTRATION_ITERATIONS = 15

# Maximum tokens for AI responses
MAX_RESPONSE_TOKENS = 4096

# Truncate conversations beyond this many messages (~20 user-assistant exchanges)
MAX_CONVERSATION_MESSAGES = 40

# Retry configuration for API calls
MAX_API_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 1.0

# =============================================================================
# Kubectl Execution
# =============================================================================

# Default timeout for kubectl commands (seconds)
DEFAULT_KUBECTL_TIMEOUT = 30

# Maximum output characters before truncation
MAX_KUBECTL_OUTPUT_CHARS = 50000

# =============================================================================
# WebSocket
# =============================================================================

# Ping interval for WebSocket connections (seconds)
WEBSOCKET_PING_INTERVAL = 30

# Maximum reconnection attempts for WebSocket
MAX_WEBSOCKET_RECONNECT_ATTEMPTS = 5

# =============================================================================
# Session
# =============================================================================

# Maximum length for session titles
MAX_SESSION_TITLE_LENGTH = 60

# Minimum word boundary position for title truncation
MIN_TITLE_TRUNCATE_POSITION = 40

# =============================================================================
# Kubernetes Naming
# =============================================================================

# Maximum length for Kubernetes names (RFC 1123)
MAX_K8S_NAME_LENGTH = 253

# Maximum length for Kubernetes labels
MAX_K8S_LABEL_LENGTH = 63

# =============================================================================
# OIDC
# =============================================================================

OIDC_STATE_TTL_SECONDS = 600

# =============================================================================
# External Access Connectors
# =============================================================================

# TTL for a pending connector OAuth handshake (state + PKCE verifier)
CONNECTOR_STATE_TTL_SECONDS = 600

# Default timeout for connector tool execution (seconds)
CONNECTOR_TOOL_TIMEOUT = 30

# =============================================================================
# OAuth2 MCP brokering
# =============================================================================

# TTL for a pending OAuth2 MCP handshake (state + PKCE verifier)
MCP_OAUTH_STATE_TTL_SECONDS = 600

# Timeout for every outbound OAuth call (discovery, token, DCR, revoke)
MCP_OAUTH_HTTP_TIMEOUT = 10.0
