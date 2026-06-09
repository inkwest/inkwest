import { Cluster, Message, PromptTemplate, SearchResponse, Session, SessionWithMessages } from '@/types/api';

export const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

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

// Clusters
interface PaginatedClusters {
  items: Cluster[];
  total: number;
}

export async function getClusters(): Promise<Cluster[]> {
  const response = await fetchAPI<PaginatedClusters>('/api/clusters');
  return response.items;
}

// Prompt templates (read-only; consumed by the chat palette)
interface PaginatedPromptTemplates {
  items: PromptTemplate[];
  total: number;
}

export async function getPromptTemplates(): Promise<PromptTemplate[]> {
  const response = await fetchAPI<PaginatedPromptTemplates>('/api/prompt-templates');
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

export async function updateSession(id: string, data: { title?: string }): Promise<Session> {
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
export async function retryMessage(sessionId: string, messageId: string): Promise<Message> {
  return fetchAPI(`/api/sessions/${sessionId}/messages/${messageId}/retry`, {
    method: 'POST',
  });
}

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

export async function activateCoralogix(sessionId: string): Promise<ConnectorStatus> {
  return fetchAPI(`/api/connectors/${sessionId}/coralogix/activate`, { method: 'POST' });
}

export async function getAwsOptions(
  sessionId: string
): Promise<{ accounts: { account_id: string; account_name: string }[]; roles: { account_id: string; role_name: string }[] }> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/options`);
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
