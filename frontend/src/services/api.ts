import {
  Cluster,
  ConfigExport,
  Message,
  PreferencesDocument,
  PreferencesResponse,
  Skill,
  SearchResponse,
  Session,
  SessionWithMessages,
  UserSkill,
} from '@/types/api';
// Deliberate service→store edge: the retry loop is the single place that knows the
// backend is unreachable, so it drives the global banner directly via zustand's
// off-React getState() (no hook/render dependency, no import cycle). Other services
// stay store-free; this is the one intentional exception.
import { useBackendStore } from '@/store/backend';
import { jitteredBackoff } from '@/lib/backoff';

// `??` not `||`: '' (set but empty) means same-origin — the combined prod
// image serves the SPA from FastAPI itself, so relative URLs must survive.
export const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

// AWS/ALB gateway statuses we retry for ANY verb with exponential backoff.
//   503 = no healthy target / surge-queue full → request never dispatched.
//   502 = target connection reset mid-request (a pod torn down on deploy) → not
//         completed by the app.
//   504 = gateway timeout. In this deployment a 504 overwhelmingly means the
//         backend is bouncing (slow death during a restart), so we retry it too.
// CAVEAT: a 504 can also mean the app received the request and was merely slow —
// so a retried non-idempotent call (POST/PUT/DELETE) could, rarely, be applied
// twice. A mid-flight network error (connection reset) on a mutation carries the
// same small risk and is retried under the same rationale. Accepted trade-off for
// resilience during restarts. (Client-side TIMEOUTS, by contrast, never retry a
// mutation — see TIMEOUT_SAFE_METHODS — because there the request is far more
// likely still running server-side.)
const RETRYABLE_STATUSES = new Set([502, 503, 504]);

// Methods safe to abort-and-retry on a client-side timeout. A timeout fabricates
// a failure the server never reported, so retrying it is only safe for idempotent
// reads — a non-idempotent call (POST/PUT/DELETE) may still be running on the
// server (e.g. the slow AWS enumerate/STS-mint endpoints), and replaying it could
// duplicate the side effect. Mutations therefore get NO client timeout: they keep
// infinite patience and rely on gateway-status / network-error retry instead.
const TIMEOUT_SAFE_METHODS = new Set(['GET', 'HEAD']);

// Generous cap: ~up to a minute of backend downtime is absorbed silently, after
// which the error surfaces to the caller so the UI isn't wedged forever.
const MAX_RETRIES = 12;
const RETRY_BASE_MS = 500;
const RETRY_MAX_MS = 5000;
// Per-attempt ceiling so a hung backend (socket accepted, no response) is treated
// as a failure and retried instead of hanging forever. Applies to idempotent
// reads only (see TIMEOUT_SAFE_METHODS); attachment transfers opt out entirely.
const ATTEMPT_TIMEOUT_MS = 30000;
// Mutations can't be timeout-RETRIED (duplicate-replay risk), but they shouldn't
// spin forever on a hung backend either. This is a timeout-to-FAIL: after this
// long with no response, reject (no replay) so the caller surfaces an error. Set
// well above the slowest legitimate mutation — the AWS access-token regen +
// account listing — so a slow-but-alive request is never killed. Attachment
// uploads opt out entirely (they pass no failTimeoutMs; can legitimately exceed).
const MUTATION_TIMEOUT_MS = 300000; // 5 minutes

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// fetch wrapper that transparently rides out a backend restart. A retryable
// gateway status (per verb) or a network/timeout failure means the backend is
// down or bouncing; we back off and retry, surfacing the state via the global
// backend store (refcounted: the banner shows while ANY call is backing off).
// Resolves with the first response that actually came from the app — including
// 4xx/5xx emitted by the app itself, which the caller handles. `init.signal` (if
// any) aborts the whole loop immediately (navigation/unmount).
async function fetchWithRetry(
  url: string,
  init: RequestInit,
  opts: { timeoutMs?: number; failTimeoutMs?: number } = {},
): Promise<Response> {
  const backend = useBackendStore.getState();
  const callerSignal = init.signal ?? undefined;
  const method = (init.method ?? 'GET').toUpperCase();
  const isSafe = TIMEOUT_SAFE_METHODS.has(method);
  // Idempotent reads get a per-attempt timeout that ABORTS-AND-RETRIES (a hung
  // read is safe to replay). Mutations get a much longer timeout-to-FAIL: they
  // must never be replayed, but should still surface an error instead of spinning
  // forever on a hung backend.
  const timeoutMs = opts.timeoutMs && isSafe ? opts.timeoutMs : undefined;
  const failTimeoutMs = opts.failTimeoutMs && !isSafe ? opts.failTimeoutMs : undefined;
  let attempt = 0;
  let holding = false;
  // Hold a refcount on the global "reconnecting" banner only while THIS call is
  // actually backing off; released in the outer finally on every exit path.
  const hold = () => {
    if (!holding) {
      holding = true;
      backend.beginRetry();
    }
  };
  // Flag the backend down and back off, then the loop retries. Shared by the
  // gateway-status and network/timeout retry paths (identical otherwise).
  const backoff = async () => {
    hold();
    await sleep(jitteredBackoff(attempt++, { baseMs: RETRY_BASE_MS, maxMs: RETRY_MAX_MS }));
  };
  try {
    for (;;) {
      // Honor a signal that was already aborted before we got here (addEventListener
      // would never fire for a past 'abort').
      if (callerSignal?.aborted) throw new DOMException('Aborted', 'AbortError');
      // Fresh controller per attempt: fires on timeout, and chains the caller's
      // signal so an unmount aborts the in-flight request right away.
      const ctrl = new AbortController();
      const onCallerAbort = () => ctrl.abort();
      callerSignal?.addEventListener('abort', onCallerAbort, { once: true });
      const timer = timeoutMs ? setTimeout(() => ctrl.abort(), timeoutMs) : undefined;
      // A fatal (mutation) timeout aborts the fetch but, unlike the retry timeout,
      // must NOT be replayed — the flag tells the catch to rethrow, not loop.
      let fatalTimeout = false;
      const failTimer = failTimeoutMs
        ? setTimeout(() => {
            fatalTimeout = true;
            ctrl.abort();
          }, failTimeoutMs)
        : undefined;
      try {
        const response = await fetch(url, { ...init, signal: ctrl.signal });
        if (RETRYABLE_STATUSES.has(response.status) && attempt < MAX_RETRIES) {
          await backoff();
          continue;
        }
        // A reachable app response (success or app-level error) is ground truth
        // that the backend is up: clear the banner regardless of other holders.
        if (!RETRYABLE_STATUSES.has(response.status)) backend.reachable();
        return response;
      } catch (e) {
        // Caller aborted (navigation/unmount): stop, do not retry.
        if (callerSignal?.aborted) throw e;
        // Mutation hung past its fail-timeout: surface the error, never replay.
        if (fatalTimeout) throw e;
        // Timeout or network failure (connection refused / DNS / TLS, or the hung
        // backend we just aborted): retry up to the cap, then rethrow.
        if (attempt < MAX_RETRIES) {
          await backoff();
          continue;
        }
        throw e;
      } finally {
        if (timer) clearTimeout(timer);
        if (failTimer) clearTimeout(failTimer);
        callerSignal?.removeEventListener('abort', onCallerAbort);
      }
    }
  } finally {
    // Release our banner hold on every exit (resolve / give-up / abort) so an
    // exhausted retry doesn't leave the banner stuck on with no holder.
    if (holding) backend.endRetry();
  }
}

// Carries the HTTP status so callers can branch on it (e.g. a 409 "re-initiate")
// rather than string-matching the detail message. Extends Error so existing
// `(e as Error).message` consumers keep working unchanged.
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export function wsBaseUrl(): string {
  const configured = import.meta.env.VITE_WS_URL;
  if (configured) return configured;
  if (configured === '') {
    // Same-origin: derive from the page; WebSocket() rejects relative URLs.
    return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`;
  }
  return 'ws://localhost:8000';
}

export async function fetchAPI<T>(
  endpoint: string,
  options?: RequestInit
): Promise<T> {
  // credentials:'include' sends the HttpOnly session cookie cross-origin (dev
  // SPA :3000 → API :8000); same-origin in the combined prod image.
  const response = await fetchWithRetry(
    `${API_URL}${endpoint}`,
    {
      ...options,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...options?.headers,
      },
    },
    { timeoutMs: ATTEMPT_TIMEOUT_MS, failTimeoutMs: MUTATION_TIMEOUT_MS },
  );

  if (!response.ok) {
    let detail = `API error: ${response.status}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      // response body not JSON
    }
    throw new ApiError(response.status, detail);
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
  const response = await fetchWithRetry(`${API_URL}/api/sessions/${sessionId}/attachments`, {
    method: 'POST',
    credentials: 'include',
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

// Fetch an attachment's bytes with auth (an <img src> can't carry the session
// cookie cross-origin), so callers turn the blob into an object URL for thumbnails.
export async function fetchAttachmentBlob(sessionId: string, filename: string): Promise<Blob> {
  const response = await fetchWithRetry(
    `${API_URL}/api/sessions/${sessionId}/attachments/${encodeURIComponent(filename)}`,
    { credentials: 'include' },
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
  /** Enabled engines that can run this model (Claude → any; others → agent engines). */
  engines: string[];
  /** Input modalities from the live catalog (TEXT/IMAGE/VIDEO/SPEECH). "IMAGE" ⇒ the
   *  picker shows the image icon and uploads are accepted. Empty = text-only/unknown. */
  input_modalities: string[];
}

export interface AppConfig {
  /** Default model (ID) applied when a session has no explicit choice. */
  model: string;
  /** The engine the default model runs on (admin choice, else resolved heuristically). */
  default_engine: string;
  /** Engines enabled in this deployment; >1 ⇒ the picker shows Engine·Model pairs. */
  engines: string[];
  /** Admin-enabled models users may pick per session. */
  available_models: ModelInfo[];
  /** File attachments usable (requires the sandbox/claude-cli). Gates the attach UI. */
  attachments_enabled: boolean;
}

/** Display name for a model ID; falls back to the raw ID when unknown. */
export function modelLabel(models: ModelInfo[], id: string): string {
  return models.find((m) => m.id === id)?.label ?? id;
}

/** Input modalities for a model ID (undefined when unknown) — drives the picker's
 *  capability icons. `id` may be null so callers can pass `pin ?? default` directly. */
export function modelModalities(models: ModelInfo[], id: string | null): string[] | undefined {
  return models.find((m) => m.id === id)?.input_modalities;
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
  build_timestamp: string;
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
export interface PaginatedSessions {
  items: Session[];
  total: number;
  page: number;
  page_size: number;
}

export type SessionSort = 'manual' | 'activity' | 'created';
export type SessionOrder = 'asc' | 'desc';
export type ShareFilter = 'shared_with_me' | 'shared_by_me' | 'private';

export interface SessionListParams {
  page?: number;
  page_size?: number;
  sort?: SessionSort;
  order?: SessionOrder;
  share?: ShareFilter[];
  /** ISO cutoff: keep only sessions active at/after this instant. */
  activity_after?: string;
  /** ISO cutoff: keep only sessions active at/before this instant. */
  activity_before?: string;
}

export async function getSessions(params: SessionListParams = {}): Promise<PaginatedSessions> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  if (params.sort) qs.set('sort', params.sort);
  if (params.order) qs.set('order', params.order);
  for (const s of params.share ?? []) qs.append('share', s);
  if (params.activity_after) qs.set('activity_after', params.activity_after);
  if (params.activity_before) qs.set('activity_before', params.activity_before);
  const query = qs.toString();
  return fetchAPI<PaginatedSessions>(`/api/sessions${query ? `?${query}` : ''}`);
}

export async function createSession(data: {
  cluster_id?: string;
  system_prompt_id?: string;
  system_prompt_custom?: string;
  model?: string;
  engine?: string;
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
  data: { title?: string; model?: string | null; engine?: string | null }
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

// WebSocket ticket. `signal` lets a disconnecting socket abort an in-flight
// ticket fetch (incl. its retry loop) instead of leaving it running for ~1 min.
export async function fetchWsTicket(signal?: AbortSignal): Promise<string> {
  const { ticket } = await fetchAPI<{ ticket: string }>('/api/auth/ws-ticket', {
    method: 'POST',
    signal,
  });
  return ticket;
}

// Dev-only (OIDC off): impersonate a local user for testing multi-user features.
// Pass '' to reset to dev@localhost. 404s under OIDC. Callers reload afterwards so
// REST + every WebSocket re-authenticate as the new identity.
export async function devSwitchUser(user: string): Promise<{ acting_as: string }> {
  return fetchAPI(`/api/auth/dev-switch?user=${encodeURIComponent(user)}`);
}

// Search
export async function searchGlobal(query: string): Promise<SearchResponse> {
  return fetchAPI(`/api/search?q=${encodeURIComponent(query)}`);
}

// ---------------------------------------------------------------------------
// Conversation sharing (read-only): user invites + revokable anonymous links.
// ---------------------------------------------------------------------------
import {
  Participant,
  PendingInvite,
  ShareLink,
  SharedConversation,
  UserSummary,
} from '@/types/api';

/** System users the caller can share with (excludes self). Drives the share picker. */
export async function getShareableUsers(): Promise<UserSummary[]> {
  return fetchAPI('/api/users');
}

/** Invite a user to a conversation (read-only). */
export async function shareSession(sessionId: string, userId: string): Promise<Participant> {
  return fetchAPI(`/api/sessions/${sessionId}/shares`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  });
}

/** Current participants + pending invitees of a conversation (owner only). */
export async function getSessionShares(sessionId: string): Promise<Participant[]> {
  return fetchAPI(`/api/sessions/${sessionId}/shares`);
}

/** Revoke a participant / pending invitee. */
export async function revokeShare(sessionId: string, userId: string): Promise<void> {
  await fetchAPI(`/api/sessions/${sessionId}/shares/${userId}`, { method: 'DELETE' });
}

/** Invitations addressed to the current user (popup recovery on load). */
export async function getPendingInvites(): Promise<PendingInvite[]> {
  return fetchAPI('/api/shares/pending');
}

/** Accept or decline an invitation. */
export async function respondToInvite(
  shareId: string,
  accept: boolean
): Promise<{ status: string; session_id: string }> {
  return fetchAPI(`/api/shares/${shareId}/respond`, {
    method: 'POST',
    body: JSON.stringify({ accept }),
  });
}

/** Mint a revokable anonymous read-only link. */
export async function createShareLink(sessionId: string, label?: string): Promise<ShareLink> {
  return fetchAPI(`/api/sessions/${sessionId}/share-links`, {
    method: 'POST',
    body: JSON.stringify({ label: label ?? null }),
  });
}

export async function getShareLinks(sessionId: string): Promise<ShareLink[]> {
  return fetchAPI(`/api/sessions/${sessionId}/share-links`);
}

export async function revokeShareLink(sessionId: string, linkId: string): Promise<void> {
  await fetchAPI(`/api/sessions/${sessionId}/share-links/${linkId}`, { method: 'DELETE' });
}

/** Leave a conversation shared with you (removes your own access; owner's copy stays). */
export async function leaveShare(sessionId: string): Promise<void> {
  await fetchAPI(`/api/sessions/${sessionId}/share`, { method: 'DELETE' });
}

/** Public, no-auth read-only view of a conversation via a share-link token. */
export async function getSharedConversation(token: string): Promise<SharedConversation> {
  return fetchAPI(`/api/shared/${token}`);
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

// Live progress of the slow AWS phases. phase "idle" → nothing running;
// "enumerating" → SSO accounts listed; "granting" → STS profiles minted;
// "discovering" → EKS clusters discovered; "done" → discovery settled (terminal, lingers
// a few seconds then reaped to "idle"). done/total scopes the active phase.
export async function getAwsEnumProgress(
  sessionId: string
): Promise<{ phase: string; done: number; total: number }> {
  return fetchAPI(`/api/connectors/${sessionId}/aws/options/progress`);
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
}

// auth_status: static | unavailable | not_connected | connected | needs_auth
export type McpAuthStatus =
  | 'static'
  | 'unavailable'
  | 'not_connected'
  | 'connected'
  | 'needs_auth';

export interface McpServerEntry {
  key: string;
  kind: 'static' | 'oauth2';
  auth_status: McpAuthStatus;
}

export interface McpCatalog {
  // `keys` keeps the pre-OAuth contract (static, injectable keys). `servers` is the
  // OAuth-aware superset with per-key auth status.
  keys: string[];
  servers: McpServerEntry[];
}

export async function getMcpCatalog(): Promise<McpCatalog> {
  return fetchAPI('/api/mcp/catalog');
}

/** Begin an OAuth2 MCP connect: returns the authorization-server URL for the popup. */
export async function startMcpAuth(key: string): Promise<{ authorize_url: string }> {
  return fetchAPI(`/api/mcp/${encodeURIComponent(key)}/auth/start`, { method: 'POST' });
}

/** Revoke the current user's grant for an OAuth2 MCP server. Returns the refreshed catalog. */
export async function disconnectMcpAuth(key: string): Promise<McpCatalog> {
  return fetchAPI(`/api/mcp/${encodeURIComponent(key)}/auth`, { method: 'DELETE' });
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

// MCP per-tool permission policy (tool enumeration + verdicts). See docs/HANDOFF-mcp-tool-permissions.md.
export type McpVerdict = 'allow' | 'deny' | 'ask';

export interface McpToolAnnotations {
  title?: string | null;
  read_only?: boolean | null;
  destructive?: boolean | null;
  idempotent?: boolean | null;
  open_world?: boolean | null;
}

export interface McpToolInfo {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  // MCP behavior hints; null/absent for unannotated tools (→ Interactive bucket).
  annotations?: McpToolAnnotations | null;
}

export interface McpServerTools {
  key: string;
  tools: McpToolInfo[];
  fetched_at: string | null;
}

export interface McpPolicyEntry {
  server_key: string;
  tool: string; // a tool name, or '*' for the server-wide default
  verdict: McpVerdict;
}

export interface McpPolicySet {
  policies: McpPolicyEntry[];
}

/** A server's tools (from the cache; enumerated live on first open or when refresh=true). */
export async function getMcpServerTools(key: string, refresh = false): Promise<McpServerTools> {
  const q = refresh ? '?refresh=true' : '';
  return fetchAPI(`/api/mcp/${encodeURIComponent(key)}/tools${q}`);
}

/** The current user's global per-tool verdicts (defaults inherited by every conversation). */
export async function getMyMcpPolicy(): Promise<McpPolicySet> {
  return fetchAPI('/api/me/mcp-policy');
}

/** Replace the user's global verdicts for one server (full-state per server). */
export async function setMyMcpPolicy(
  serverKey: string,
  entries: { tool: string; verdict: McpVerdict }[]
): Promise<McpPolicySet> {
  return fetchAPI('/api/me/mcp-policy', {
    method: 'PUT',
    body: JSON.stringify({ server_key: serverKey, entries }),
  });
}

/** A conversation's per-tool override verdicts. */
export async function getSessionMcpPolicy(sessionId: string): Promise<McpPolicySet> {
  return fetchAPI(`/api/sessions/${sessionId}/mcp/policy`);
}

/** Replace a conversation's verdicts for one server (full-state per server). */
export async function setSessionMcpPolicy(
  sessionId: string,
  serverKey: string,
  entries: { tool: string; verdict: McpVerdict }[]
): Promise<McpPolicySet> {
  return fetchAPI(`/api/sessions/${sessionId}/mcp/policy`, {
    method: 'PUT',
    body: JSON.stringify({ server_key: serverKey, entries }),
  });
}

// ---------------------------------------------------------------------------
// Current-user self-service: preferences + personal skills.
// All scoped to the authenticated user — no admin role.
// ---------------------------------------------------------------------------

export async function getMyPreferences(): Promise<PreferencesResponse> {
  return fetchAPI<PreferencesResponse>('/api/me/preferences');
}

export async function updateMyPreferences(
  document: PreferencesDocument
): Promise<PreferencesResponse> {
  return fetchAPI<PreferencesResponse>('/api/me/preferences', {
    method: 'PUT',
    body: JSON.stringify({ document }),
  });
}

export async function getMySkills(): Promise<UserSkill[]> {
  return fetchAPI<UserSkill[]>('/api/me/skills');
}

export async function createMySkill(content: string): Promise<UserSkill> {
  return fetchAPI<UserSkill>('/api/me/skills', {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
}

export async function updateMySkill(
  id: string,
  data: { content?: string; is_active?: boolean }
): Promise<UserSkill> {
  return fetchAPI<UserSkill>(`/api/me/skills/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  });
}

export async function deleteMySkill(id: string): Promise<void> {
  return fetchAPI<void>(`/api/me/skills/${id}`, { method: 'DELETE' });
}

export async function overrideAdminSkill(adminName: string): Promise<UserSkill> {
  return fetchAPI<UserSkill>(`/api/me/skills/override/${encodeURIComponent(adminName)}`, {
    method: 'POST',
  });
}

export async function exportMyConfig(): Promise<ConfigExport> {
  return fetchAPI<ConfigExport>('/api/me/export');
}

export async function importMyConfig(
  payload: ConfigExport,
  replaceAll: boolean
): Promise<PreferencesResponse> {
  return fetchAPI<PreferencesResponse>(`/api/me/import?replace_all=${replaceAll}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
