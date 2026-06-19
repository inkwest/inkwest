import { Cluster, Message, Skill, SearchResponse, Session, SessionWithMessages } from '@/types/api';

// `??` not `||`: '' (set but empty) means same-origin — the combined prod
// image serves the SPA from FastAPI itself, so relative URLs must survive.
export const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

export function wsBaseUrl(): string {
  const configured = import.meta.env.VITE_WS_URL;
  if (configured) return configured;
  if (configured === '') {
    // Same-origin: derive from the page; WebSocket() rejects relative URLs.
    return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`;
  }
  return 'ws://localhost:8000';
}

function getAuthHeaders(): HeadersInit {
  const token = localStorage.getItem('session_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function fetchAPI<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  const response = await fetch(`${API_URL}${endpoint}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...getAuthHeaders(),
      ...options?.headers,
    },
  });

  if (!response.ok) {
    let detail = `API error: ${response.status}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      // response body not JSON
    }
    throw new Error(detail);
  }

  if (response.status === 204) return undefined as unknown as T;

  return response.json();
}

// ---------------------------------------------------------------------------
// Attachments — multipart upload + blob fetch can't go through fetchAPI (it
// forces application/json and parses JSON). The browser sets the multipart
// boundary itself, so we must NOT set Content-Type on the upload.
// ---------------------------------------------------------------------------

export async function uploadAttachments(
  sessionId: string,
  files: File[],
): Promise<import('@/types/api').Attachment[]> {
  const form = new FormData();
  for (const f of files) form.append('files', f);
  const response = await fetch(`${API_URL}/api/sessions/${sessionId}/attachments`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: form,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body?.detail || `Upload failed: ${response.status}`);
  }
  return response.json();
}

export async function deleteAttachment(sessionId: string, filename: string): Promise<void> {
  await fetchAPI(`/api/sessions/${sessionId}/attachments/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
  });
}

// Fetch an attachment's bytes with auth (an <img src> can't carry the bearer
// token), so callers turn the blob into an object URL for thumbnails.
export async function fetchAttachmentBlob(sessionId: string, filename: string): Promise<Blob> {
  const response = await fetch(
    `${API_URL}/api/sessions/${sessionId}/attachments/${encodeURIComponent(filename)}`,
    { headers: getAuthHeaders() },
  );
  if (!response.ok) throw new Error(`Failed to load attachment: ${response.status}`);
  return response.blob();
}

// App config
export interface ModelInfo {
  /** Internal model ID, e.g. "claude-opus-4-8". */
  id: string;
  /** Human-friendly display name, e.g. "Opus 4.8". */
  label: string;
  /** Older generation, kept for cost reporting — not recommended for use. */
  legacy: boolean;
}

export interface AppConfig {
  provider: string;
  /** Default model (ID) applied when a session has no explicit choice. */
  model: string;
  /** Admin-enabled models users may pick per session. */
  available_models: ModelInfo[];
  /** File attachments usable (requires the sandbox/claude-cli). Gates the attach UI. */
  attachments_enabled: boolean;
}

/** Display name for a model ID; falls back to the raw ID when unknown. */
export function modelLabel(models: ModelInfo[], id: string): string {
  return models.find((m) => m.id === id)?.label ?? id;
}

export async function getAppConfig(): Promise<AppConfig> {
  return fetchAPI('/api/config');
}

// Build metadata of the running backend (see backend /version). Used to detect a
// newer deployment after a WebSocket reconnect. Not under /api — the health
// router is mounted at the root.
export interface VersionInfo {
  version: string;
  commit: string;
}

export async function getVersion(): Promise<VersionInfo> {
  return fetchAPI('/version');
}

// Clusters
interface PaginatedClusters {
  items: Cluster[];
  total: number;
}

export async function getClusters(): Promise<Cluster[]> {
  const response = await fetchAPI<PaginatedClusters>('/api/clusters');
  return response.items;
}

// Skills (read-only; consumed by the chat palette)
interface PaginatedSkills {
  items: Skill[];
  total: number;
}

export async function getSkills(): Promise<Skill[]> {
  const response = await fetchAPI<PaginatedSkills>('/api/skills');
  return response.items;
}

export async function createCluster(data: {
  name: string;
  display_name: string;
  kubeconfig_path: string;
}): Promise<Cluster> {
  return fetchAPI('/api/clusters', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

// Sessions
interface PaginatedSessions {
  items: Session[];
  total: number;
  page: number;
  page_size: number;
}

export async function getSessions(): Promise<Session[]> {
  const response = await fetchAPI<PaginatedSessions>('/api/sessions');
  return response.items;
}

export async function createSession(data: {
  cluster_id?: string;
  system_prompt_id?: string;
  system_prompt_custom?: string;
  model?: string;
}): Promise<Session> {
  return fetchAPI('/api/sessions', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export async function getSession(id: string): Promise<SessionWithMessages> {
  return fetchAPI(`/api/sessions/${id}`);
}

export async function deleteSession(id: string): Promise<void> {
  await fetchAPI(`/api/sessions/${id}`, { method: 'DELETE' });
}

export async function updateSession(
  id: string,
  data: { title?: string; model?: string | null }
): Promise<Session> {
  return fetchAPI(`/api/sessions/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

// Persist the sidebar arrangement: full top-to-bottom list of session ids.
export async function reorderSessions(sessionIds: string[]): Promise<void> {
  await fetchAPI('/api/sessions/reorder', {
    method: 'POST',
    body: JSON.stringify({ session_ids: sessionIds }),
  });
}

// Messages
export async function getFailedMessages(sessionId: string): Promise<Message[]> {
  return fetchAPI(`/api/sessions/${sessionId}/failed-messages`);
}

// WebSocket ticket
export async function fetchWsTicket(): Promise<string> {
  const { ticket } = await fetchAPI<{ ticket: string }>('/api/auth/ws-ticket', {
    method: 'POST',
  });
  return ticket;
}

// Search
export async function searchGlobal(query: string): Promise<SearchResponse> {
  return fetchAPI(`/api/search?q=${encodeURIComponent(query)}`);
}

// External access connectors
import { ConnectorName, ConnectorStatus, PriorGrant } from '@/types/connectors';

export async function getConnectorStatus(sessionId: string): Promise<ConnectorStatus[]> {
  return fetchAPI(`/api/connectors/${sessionId}/status`);
}

// Connectors this session previously granted — drives the "Renew access" action.
export async function getRenewable(sessionId: string): Promise<PriorGrant[]> {
  return fetchAPI(`/api/connectors/${sessionId}/renewable`);
}

// Silent GitLab renewal from the held refresh token (409 → caller re-initiates OAuth).
export async function gitlabRenew(sessionId: string): Promise<ConnectorStatus> {
  return fetchAPI(`/api/connectors/${sessionId}/gitlab/renew`, { method: 'POST' });
}

// After a fresh AWS device auth, re-apply the session's previously-saved selection.
export async function awsRenewSelect(sessionId: string): Promise<ConnectorStatus> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/renew-select`, { method: 'POST' });
}

export async function initiateConnector(
  sessionId: string,
  connector: ConnectorName
): Promise<{ redirect_url: string }> {
  return fetchAPI(`/api/connectors/${sessionId}/${connector}/initiate`, { method: 'POST' });
}

// Grant this session from the user's warm platform auth (no login flow).
// AWS → needs_selection (picker opens directly); GitLab → connected.
// 409 → no warm grant: the caller falls back to the regular login flow.
export async function grantFromWarm(
  sessionId: string,
  connector: ConnectorName
): Promise<ConnectorStatus> {
  return fetchAPI(`/api/connectors/${sessionId}/${connector}/grant-from-warm`, {
    method: 'POST',
  });
}


export async function getAwsOptions(
  sessionId: string
): Promise<{ accounts: { account_id: string; account_name: string }[]; roles: { account_id: string; role_name: string }[] }> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/options`);
}

// Force a re-enumeration of the SSO accounts/roles (bypasses the warm cache).
export async function refreshAwsOptions(
  sessionId: string
): Promise<{ accounts: { account_id: string; account_name: string }[]; roles: { account_id: string; role_name: string }[] }> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/options/refresh`, { method: 'POST' });
}

export async function selectAwsRoles(
  sessionId: string,
  selections: { account_id: string; role_name: string }[]
): Promise<ConnectorStatus> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/select`, {
    method: 'POST',
    body: JSON.stringify({ selections }),
  });
}

export async function revokeConnector(
  sessionId: string,
  connector: ConnectorName
): Promise<ConnectorStatus> {
  return fetchAPI(`/api/connectors/${sessionId}/${connector}/revoke`, { method: 'POST' });
}

// AWS device authorization flow (no redirect — loopback-only public client)
export interface AwsDeviceStart {
  user_code: string;
  verification_uri: string;
  verification_uri_complete: string;
  interval: number;
  expires_in: number;
}

export async function startAwsDevice(sessionId: string): Promise<AwsDeviceStart> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/device/start`, { method: 'POST' });
}

export async function pollAwsDevice(
  sessionId: string
): Promise<{ status: string; detail?: string | null }> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/device/poll`, { method: 'POST' });
}

// MCP catalog + per-conversation selection
interface SessionMcpSelection {
  enabled: string[];
  expires_at?: string | null;
}

export async function getMcpCatalog(): Promise<{ keys: string[] }> {
  return fetchAPI('/api/mcp/catalog');
}

export async function getSessionMcp(sessionId: string): Promise<SessionMcpSelection> {
  return fetchAPI(`/api/sessions/${sessionId}/mcp`);
}

export async function setSessionMcp(
  sessionId: string,
  enabled: string[]
): Promise<SessionMcpSelection> {
  return fetchAPI(`/api/sessions/${sessionId}/mcp`, {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  });
}
