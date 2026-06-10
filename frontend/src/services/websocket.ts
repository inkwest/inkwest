import { ChatEvent, SendMessage } from '@/types/events';
import { fetchWsTicket, wsBaseUrl } from '@/services/api';

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

  constructor(sessionId: string) {
    this.sessionId = sessionId;
  }

  async connect(): Promise<void> {
    if (this._disconnected) return;
    const wsUrl = wsBaseUrl();
    const token = localStorage.getItem('session_token');
    let protocols: string[] = ['inkwest'];
    if (token) {
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
    }
    this.ws = new WebSocket(`${wsUrl}/ws/chat/${this.sessionId}`, protocols);

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      this.reconnectAttempts = 0;
      this.startPingInterval();
      this.notifyHandlers({ type: 'connected' } as unknown as ChatEvent);
    };

    this.ws.onmessage = (event) => {
      try {
        const data: ChatEvent = JSON.parse(event.data);
        this.notifyHandlers(data);
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    this.ws.onclose = (event) => {
      console.log('WebSocket closed:', event.code);
      this.stopPingInterval();
      this.notifyHandlers({ type: 'disconnected' } as unknown as ChatEvent);

      if (event.code === 4001) {
        localStorage.removeItem('session_token');
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

  send(message: SendMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    } else {
      console.warn('WebSocket not connected, cannot send message');
    }
  }

  sendMessage(content: string): void {
    this.send({ type: 'message', content });
  }

  sendStop(): void {
    this.send({ type: 'stop' });
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
