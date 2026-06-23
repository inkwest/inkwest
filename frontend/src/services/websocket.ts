import { ChatEvent, SendMessage } from '@/types/events';
import { fetchWsTicket, wsBaseUrl } from '@/services/api';
import { dekReply } from '@/lib/mcpDek';

type EventHandler = (event: ChatEvent) => void;

export class ChatWebSocket {
  private ws: WebSocket | null = null;
  private sessionId: string;
  private handlers: Set<EventHandler> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private pingInterval: number | null = null;
  private reconnectTimer: number | null = null;
  private _disconnected = false;
  private pendingSends: SendMessage[] = [];

  constructor(sessionId: string) {
    this.sessionId = sessionId;
  }

  async connect(): Promise<void> {
    if (this._disconnected) return;
    const wsUrl = wsBaseUrl();
    // The ticket POST is cookie-authed (dev mode returns the dev user); the WS
    // handshake can't carry the cookie reliably, so it rides the subprotocol.
    let protocols: string[] = ['inkwest'];
    try {
      const ticket = await fetchWsTicket();
      if (this._disconnected) return;
      protocols = ['inkwest', `ticket|${ticket}`];
    } catch {
      if (this._disconnected) return;
      this.notifyHandlers({ type: 'error', content: 'Failed to authenticate connection' });
      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        this.scheduleReconnect();
      }
      return;
    }
    this.ws = new WebSocket(`${wsUrl}/ws/chat/${this.sessionId}`, protocols);

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      this.reconnectAttempts = 0;
      this.startPingInterval();
      for (const message of this.pendingSends.splice(0)) {
        this.ws?.send(JSON.stringify(message));
      }
      // Kick off the MCP DEK handshake (browser-fractioned key custody) so the server can
      // decrypt this user's MCP token blobs while connected. No-op if no oauth MCP is used.
      this.sendRaw({ type: 'mcp_dek_open' });
      this.notifyHandlers({ type: 'connected' } as unknown as ChatEvent);
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
      this.notifyHandlers({ type: 'disconnected' } as unknown as ChatEvent);

      if (event.code === 4001) {
        // Session expired/invalid — bounce to root; the AuthGuard re-checks and
        // redirects to login if the cookie is gone.
        window.location.href = '/';
        return;
      }

      if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
        this.scheduleReconnect();
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
    this.reconnectAttempts++;
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);
    console.log(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);

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
