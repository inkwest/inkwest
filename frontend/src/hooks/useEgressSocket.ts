import { useEffect, useRef, useState } from 'react';
import { fetchWsTicket, wsBaseUrl } from '@/services/api';

const WS_URL = wsBaseUrl();
const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_ATTEMPTS = 3;
const MAX_EVENTS = 500;

export interface EgressEvent {
  _id: number; // client-assigned, monotonic — stable React key across buffer slicing
  ts: number;
  decision: string; // e.g. TCP_TUNNEL, TCP_DENIED
  status: string; // e.g. 200, 403
  method: string; // CONNECT, GET, …
  host: string;
  port: string;
  bytes: number;
  client: string;
}

interface EgressState {
  events: EgressEvent[];
  connected: boolean;
  disabled: boolean; // sandbox feature is off (server closed with 4003)
}

/** squid denials surface as TCP_DENIED and/or a 4xx/5xx status on the line. */
export function isBlocked(e: EgressEvent): boolean {
  return e.decision.includes('DENIED') || e.status.startsWith('4') || e.status.startsWith('5');
}

/**
 * Subscribe to a conversation's live squid egress log (`/ws/egress/{sessionId}`).
 *
 * The proxy log is tailed from the start, so each (re)connect replays the full
 * conversation egress — we reset the buffer on connect to avoid duplicates.
 */
export function useEgressSocket(sessionId: string | undefined): EgressState {
  const [events, setEvents] = useState<EgressEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const nextIdRef = useRef(0); // monotonic across reconnects -> stable, unique keys

  useEffect(() => {
    if (!sessionId) return;
    let unmounted = false;
    let retries = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    setDisabled(false);

    async function connect() {
      if (unmounted) return;
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
      // Tail restarts from the file's start, so clear before (re)reading.
      setEvents([]);

      const token = localStorage.getItem('session_token');
      let protocols: string[] = ['inkwest'];
      if (token) {
        try {
          const ticket = await fetchWsTicket();
          if (unmounted) return;
          protocols = ['inkwest', `ticket|${ticket}`];
        } catch {
          return;
        }
      }

      const ws = new WebSocket(`${WS_URL}/ws/egress/${sessionId}`, protocols);
      wsRef.current = ws;

      ws.onopen = () => {
        retries = 0;
        setConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type !== 'egress') return;
          const entry: EgressEvent = { ...(data as EgressEvent), _id: nextIdRef.current++ };
          setEvents((prev) => {
            const next = [...prev, entry];
            return next.length > MAX_EVENTS ? next.slice(-MAX_EVENTS) : next;
          });
        } catch {
          // ignore malformed frames
        }
      };

      ws.onclose = (event) => {
        setConnected(false);
        if (event.code === 4003) {
          setDisabled(true); // feature disabled — don't retry
          return;
        }
        if (event.code === 4001 || event.code === 4004) return; // auth/not-found — don't retry
        if (unmounted || retries >= MAX_RECONNECT_ATTEMPTS) return;
        retries++;
        reconnectTimer = setTimeout(() => void connect(), RECONNECT_DELAY * retries);
      };

      ws.onerror = () => ws.close();
    }

    void connect();

    return () => {
      unmounted = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
      setConnected(false);
    };
  }, [sessionId]);

  return { events, connected, disabled };
}
