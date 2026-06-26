import { ChatEvent, SendMessage } from '@/types/events';
import { fetchWsTicket, wsBaseUrl } from '@/services/api';
import { dekReply } from '@/lib/mcpDek';
import { jitteredBackoff, createReconnectBudget, RECONNECT_BUDGET_MS } from '@/lib/backoff';
import { useBackendStore } from '@/store/backend';

type EventHandler = (event: ChatEvent) => void;

export class ChatWebSocket {
  private ws: WebSocket | null = null;
  private sessionId: string;
  private handlers: Set<EventHandler> = new Set();
  private reconnectAttempts = 0;
  // Chat reconnects faster than the sync/egress loops (3000) because a live chat
  // stream is the most latency-sensitive socket — a dropped frame mid-answer is
  // the most visible to the user.
  private reconnectDelay = 1000;
  // No attempt cap: a backend deploy/restart can outlast any fixed count, so we
  // keep reconnecting until it returns (or the budget is spent / disconnect()).
  // The delay is capped so backoff plateaus instead of growing unbounded.
  private maxReconnectDelay = 30000;
  private pingInterval: number | null = null;
  private reconnectTimer: number | null = null;
  private _disconnected = false;
  // Wall-clock give-up budget; reset on a successful open.
  private reconnectBudget = createReconnectBudget(RECONNECT_BUDGET_MS);
  private ticketAbort: AbortController | null = null;
  private pendingSends: SendMessage[] = [];
  // Cap the offline queue: a user typing through a long outage must not grow it
  // without bound. At the cap, new sends are rejected (see send()) rather than
  // evicting already-queued messages.
  private maxPendingSends = 50;

  constructor(sessionId: string) {
    this.sessionId = sessionId;
  }

  async connect(): Promise<void> {
    if (this._disconnected) return;
    const wsUrl = wsBaseUrl();
    // The ticket POST is cookie-authed (dev mode returns the dev user); the WS
    // handshake can't carry the cookie reliably, so it rides the subprotocol.
    let protocols: string[] = ['inkwest'];
    this.ticketAbort = new AbortController();
    try {
      const ticket = await fetchWsTicket(this.ticketAbort.signal);
      if (this._disconnected) return;
      protocols = ['inkwest', `ticket|${ticket}`];
    } catch {
      if (this._disconnected) return;
      this.notifyHandlers({ type: 'error', content: 'Failed to authenticate connection' });
      // Reconnect on a failed ticket POST too (backend redeploy / auth-gateway
      // blip). scheduleReconnect owns both the give-up decision (budget) and the
      // 'reconnecting' state, so this path can't strand the UI on "Reconnecting…".
      this.scheduleReconnect();
      return;
    }
    this.ws = new WebSocket(`${wsUrl}/ws/chat/${this.sessionId}`, protocols);

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      this.reconnectAttempts = 0;
      this.reconnectBudget.reset(); // recovered — grant a fresh give-up budget
      // A live socket is proof the backend is reachable — clear the global banner.
      useBackendStore.getState().reachable();
      this.startPingInterval();
      for (const message of this.pendingSends.splice(0)) {
        this.ws?.send(JSON.stringify(message));
      }
      // Kick off the MCP DEK handshake (browser-fractioned key custody) so the server can
      // decrypt this user's MCP token blobs while connected. No-op if no oauth MCP is used.
      this.sendRaw({ type: 'mcp_dek_open' });
      this.notifyHandlers({ type: 'connected' });
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (typeof data?.type === 'string' && data.type.startsWith('mcp_dek_')) {
          void this.handleDekMessage(data);
          return;
        }
        this.notifyHandlers(data as ChatEvent);
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    this.ws.onclose = (event) => {
      console.log('WebSocket closed:', event.code);
      this.stopPingInterval();
      this.notifyHandlers({ type: 'disconnected' });

      if (event.code === 4001) {
        // Session expired/invalid — bounce to root; the AuthGuard re-checks and
        // redirects to login if the cookie is gone.
        window.location.href = '/';
        return;
      }

      // Terminal: access to this session is gone, so reconnecting would just loop
      // on the same rejection (a "Connection error" storm). 4004 access denied /
      // not found, 4010 session deleted, 4011 access revoked. The sync socket
      // navigates the user out of the dead session (see useSyncSocket).
      if (event.code === 4004 || event.code === 4010 || event.code === 4011) {
        this.notifyHandlers({ type: 'reconnecting', state: false });
        return;
      }

      // No attempt cap: a backend deploy/restart can outlast any fixed count, so
      // keep reconnecting on any non-clean, non-terminal close (delay is capped).
      if (event.code !== 1000) {
        this.scheduleReconnect();
      } else {
        // Clean close (1000) — no retry pending. Drop any lingering "Reconnecting…"
        // state so the flat "Disconnected" badge stands. (Exhaustion give-up lives in
        // scheduleReconnect's budget check, not here.)
        this.notifyHandlers({ type: 'reconnecting', state: false });
      }
    };

    this.ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      this.notifyHandlers({
        type: 'error',
        content: 'Connection error',
      });
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectBudget.expired()) {
      // Budget spent: give up QUIETLY. Chat is per-session and remounts on
      // navigation (fresh budget), and the chat view surfaces its own connection
      // error — so it must not raise the GLOBAL reload banner. That is the sync
      // socket's job (the app-wide heartbeat); see useSyncSocket.
      //
      // Surface the give-up to the chat UI so its badge drops "Reconnecting…" back
      // to "Disconnected" instead of spinning forever with no retry pending. This is
      // the single give-up point for both socket-close and ticket-fetch exhaustion.
      this.notifyHandlers({ type: 'reconnecting', state: false });
      return;
    }
    this.reconnectAttempts++;
    const delay = jitteredBackoff(this.reconnectAttempts - 1, {
      baseMs: this.reconnectDelay,
      maxMs: this.maxReconnectDelay,
    });
    console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);

    // A retry is now pending — surface it so the UI distinguishes a transient blip
    // from a terminal disconnect. Cleared by the next 'connected' (onopen), the
    // budget give-up above, or a terminal/clean close in onclose.
    this.notifyHandlers({ type: 'reconnecting', state: true });

    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  private startPingInterval(): void {
    this.pingInterval = window.setInterval(() => {
      this.send({ type: 'ping' });
    }, 30000);
  }

  private stopPingInterval(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  // Returns true only when the frame was actually written to an open socket.
  // Callers that optimistically mutate UI (e.g. retry) must check this so they
  // don't fake success for a frame that was queued or dropped.
  send(message: SendMessage): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
      return true;
    }
    if (message.type === 'message') {
      // Never silently drop a user message during a connect/reconnect window —
      // park it and flush on open. Pings/stops are meaningless on a dead socket.
      // At the cap, reject THIS send (return false) rather than evicting an
      // already-queued earlier message: the caller already treats false as
      // "not delivered live", so it gets an honest signal, and messages the user
      // was previously told were queued are not silently discarded.
      if (this.pendingSends.length >= this.maxPendingSends) return false;
      this.pendingSends.push(message);
      return false;
    }
    console.warn('WebSocket not connected, cannot send message');
    return false;
  }

  private sendRaw(obj: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  /** Drive the MCP DEK handshake. The unwrap/wrap logic lives in `dekReply` (unit-tested). */
  private async handleDekMessage(data: {
    type: string;
    wrapped_dek?: string;
    wrap_iv?: string;
    dek?: string;
  }): Promise<void> {
    try {
      const reply = await dekReply(data);
      if (reply) this.sendRaw(reply);
    } catch (e) {
      console.error('MCP DEK handshake failed:', e);
    }
  }

  sendMessage(content: string, attachments?: import('@/types/api').Attachment[]): void {
    this.send({ type: 'message', content, ...(attachments?.length ? { attachments } : {}) });
  }

  sendStop(): void {
    this.send({ type: 'stop' });
  }

  /** Broadcast this client's typing state to the other participants (best-effort). */
  sendTyping(state: boolean): void {
    this.send({ type: 'typing', state });
  }

  sendRetry(): boolean {
    return this.send({ type: 'retry' });
  }

  subscribe(handler: EventHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  private notifyHandlers(event: ChatEvent): void {
    this.handlers.forEach((handler) => handler(event));
  }

  disconnect(): void {
    this._disconnected = true;
    this.pendingSends = [];
    // Abort an in-flight ticket fetch (and its retry loop) so it doesn't linger.
    this.ticketAbort?.abort();
    this.stopPingInterval();
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close(1000);
      this.ws = null;
    }
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}
