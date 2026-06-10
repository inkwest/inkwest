import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSessionsStore } from '@/store/sessions';
import { useChatStore } from '@/store/chat';
import { fetchWsTicket, wsBaseUrl } from '@/services/api';

const WS_URL = wsBaseUrl();
const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_ATTEMPTS = 5;
const PING_INTERVAL = 30000;

export function useSyncSocket() {
  const navigate = useNavigate();
  const fetchSessions = useSessionsStore((s) => s.fetchSessions);
  const fetchSession = useSessionsStore((s) => s.fetchSession);
  const currentSessionRef = useRef<string | null>(null);
  const isStreamingRef = useRef(false);

  useEffect(() => {
    const unsub1 = useSessionsStore.subscribe((state) => {
      currentSessionRef.current = state.currentSession?.id ?? null;
    });
    const unsub2 = useChatStore.subscribe((state) => {
      isStreamingRef.current = state.isStreaming;
    });
    return () => { unsub1(); unsub2(); };
  }, []);

  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval>>();
  const reconnectRef = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    let unmounted = false;
    let retries = 0;

    function scheduleReconnect() {
      retries++;
      if (unmounted || retries > MAX_RECONNECT_ATTEMPTS) return;
      const delay = RECONNECT_DELAY * Math.pow(2, retries - 1);
      reconnectRef.current = setTimeout(() => void connect(), delay);
    }

    async function connect() {
      if (unmounted) return;

      // Close any existing connection to prevent orphaned sockets
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }

      const token = localStorage.getItem('session_token');
      let protocols: string[] = ['inkwest'];
      if (token) {
        try {
          const ticket = await fetchWsTicket();
          if (unmounted) return;
          protocols = ['inkwest', `ticket|${ticket}`];
        } catch {
          scheduleReconnect();
          return;
        }
      }
      const ws = new WebSocket(`${WS_URL}/ws/sync`, protocols);
      wsRef.current = ws;

      ws.onopen = () => {
        retries = 0;
        pingRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, PING_INTERVAL);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'pong') return;
          fetchSessions();
          const sid = data.session_id;
          if (data.type === 'session_deleted' && sid === currentSessionRef.current) {
            navigate('/');
            return;
          }
          if (sid && sid === currentSessionRef.current && !isStreamingRef.current) {
            fetchSession(sid);
          }
        } catch {
          // ignore
        }
      };

      ws.onclose = (event) => {
        cleanup();
        if (event.code === 4001) {
          localStorage.removeItem('session_token');
          if (!unmounted) navigate('/');
          return;
        }
        scheduleReconnect();
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    function cleanup() {
      if (pingRef.current) clearInterval(pingRef.current);
      pingRef.current = undefined;
    }

    void connect();

    return () => {
      unmounted = true;
      cleanup();
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [fetchSessions, fetchSession, navigate]);
}
