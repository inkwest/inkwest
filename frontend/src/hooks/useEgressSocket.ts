import { useEffect, useRef, useState } from 'react';
import { fetchWsTicket, wsBaseUrl } from '@/services/api';
import { useBackendStore } from '@/store/backend';
import { jitteredBackoff, createReconnectBudget, RECONNECT_BUDGET_MS } from '@/lib/backoff';

const WS_URL = wsBaseUrl();
// No attempt cap: keep retrying across a backend deploy/restart; the shared
// jittered backoff caps the delay (capped exponential, like the other WS loops).
const RECONNECT_DELAY = 3000;
const RECONNECT_MAX_DELAY = 30000;
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

/**
 * A real squid *block*: the access-control list refused the host, so squid never
 * contacted the upstream — logged as `TCP_DENIED` (with `HIER_NONE`). This is the
 * only thing the policy actually denied.
 *
 * Note: a 4xx/5xx status alone is NOT a block. A line like `TCP_TUNNEL/503 …
 * HIER_DIRECT/<ip>` means squid established the tunnel and the upstream (or the
 * connection to it) failed — an error unrelated to egress policy. Those are
 * classified by {@link isError}, not here.
 */
export function isBlocked(e: EgressEvent): boolean {
  return e.decision.includes('DENIED');
}

/**
 * A non-block failure: squid forwarded the request but the upstream returned/raised a
 * 4xx/5xx. For HTTPS (CONNECT) this only fires on squid-generated tunnel failures
 * (502/503/504), since the tunnelled inner status is invisible to squid; a genuine
 * upstream 4xx/5xx is only seen — and flagged here — on plain-HTTP traffic.
 */
export function isError(e: EgressEvent): boolean {
  return !isBlocked(e) && (e.status.startsWith('4') || e.status.startsWith('5'));
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
    const reconnectBudget = createReconnectBudget(RECONNECT_BUDGET_MS);
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    const ticketAbort = new AbortController();
    setDisabled(false);

    // Schedule a jittered reconnect, or stop once the wall-clock budget is spent.
    // Egress is a secondary observability panel, so on give-up it just stops
    // silently (panel shows disconnected) rather than raising the GLOBAL reload
    // banner — that is reserved for the sync socket, whose loss means the app is
    // actually unusable. A still-healthy app shouldn't flash "reload" because only
    // the squid-log socket couldn't reconnect.
    function scheduleReconnect() {
      if (unmounted) return;
      if (reconnectBudget.expired()) return; // budget spent — stop quietly
      reconnectTimer = setTimeout(
        () => void connect(),
        jitteredBackoff(retries++, { baseMs: RECONNECT_DELAY, maxMs: RECONNECT_MAX_DELAY }),
      );
    }

    async function connect() {
      if (unmounted) return;
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
      // Tail restarts from the file's start, so clear before (re)reading.
      setEvents([]);

      // Cookie-authed ticket POST (dev mode returns the dev user); the ticket
      // rides the WS subprotocol since the handshake can't carry the cookie.
      let protocols: string[] = ['inkwest'];
      try {
        const ticket = await fetchWsTicket(ticketAbort.signal);
        if (unmounted) return;
        protocols = ['inkwest', `ticket|${ticket}`];
      } catch {
        // Transient ticket failure (backend blip / restart): retry with backoff
        // like a dropped socket, rather than silently abandoning the panel.
        if (unmounted) return;
        scheduleReconnect();
        return;
      }

      const ws = new WebSocket(`${WS_URL}/ws/egress/${sessionId}`, protocols);
      wsRef.current = ws;

      ws.onopen = () => {
        retries = 0;
        reconnectBudget.reset(); // recovered — grant a fresh give-up budget
        // A live socket is proof the backend is reachable — clear the global banner.
        useBackendStore.getState().reachable();
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
        if (event.code === 1000) return; // app-initiated clean close (deploy=1012/1006 reconnects)
        if (unmounted) return;
        scheduleReconnect();
      };

      ws.onerror = () => ws.close();
    }

    void connect();

    return () => {
      unmounted = true;
      ticketAbort.abort(); // cancel an in-flight ticket fetch + its retry loop
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
