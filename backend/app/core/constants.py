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
# The compaction turn is the one turn that must see everything it is about to summarize,
# so it replaces the window above. Still finite: a runaway conversation would otherwise be
# sent whole to a model that will reject it.
COMPACTION_MAX_MESSAGES = 2000

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

# Max bytes read from an OAuth discovery/DCR/token response. The endpoints are URLs the
# remote server hands us (resource metadata, issuer); without a byte cap a malicious or
# compromised AS could stream a multi-GB body and OOM the single worker (the timeout bounds
# time, not size). Real metadata/token docs are a few KB; 2 MiB is generous headroom.
MCP_OAUTH_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Max request-body bytes the MCP reverse proxy will buffer. The body is buffered in RAM so
# it can be replayed on the 401-refresh retry; without a cap a large/streamed tool-call
# payload (or many concurrent ones) is an unbounded-memory blast radius on the single worker.
MCP_PROXY_MAX_BODY_BYTES = 10 * 1024 * 1024

# Max request-body bytes the LLM auth broker will buffer before forwarding to Bedrock. The
# body is buffered once (no retry, unlike the MCP proxy) only to bound single-worker memory
# under concurrency. Sized for a Converse request carrying conversation history plus inline
# base64 images (attachment limits cap an image well under this); larger than the MCP cap
# because a vision turn legitimately dwarfs a tool-call payload, still bounded against abuse.
LLM_PROXY_MAX_BODY_BYTES = 32 * 1024 * 1024

# Total wall-clock ceiling (seconds) on a single proxied Bedrock response stream. The httpx
# ``read`` timeout is only an inter-byte idle ceiling, so a buggy/malicious upstream dribbling
# bytes just under it could pin a worker slot indefinitely (slowloris) on the single replica.
# This caps the whole stream — generous enough to outlast any real turn (streaming + many tool
# calls), short enough that a held connection eventually releases its worker + FD.
LLM_PROXY_STREAM_MAX_SECONDS = 2 * 60 * 60
