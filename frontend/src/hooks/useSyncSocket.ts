import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSessionsStore } from '@/store/sessions';
import { useChatStore } from '@/store/chat';
import { useUpdateStore } from '@/store/update';
import { useInvitesStore } from '@/store/invites';
import { useBackendStore } from '@/store/backend';
import { fetchWsTicket, getVersion, wsBaseUrl } from '@/services/api';
import { jitteredBackoff, createReconnectBudget, RECONNECT_BUDGET_MS } from '@/lib/backoff';

const WS_URL = wsBaseUrl();
const RECONNECT_DELAY = 3000;
// No attempt cap: keep reconnecting across a backend deploy/restart. The delay
// is capped so backoff plateaus rather than growing without bound.
const RECONNECT_MAX_DELAY = 30000;
const PING_INTERVAL = 30000;

export function useSyncSocket() {
  const navigate = useNavigate();
  const fetchSessions = useSessionsStore((s) => s.fetchSessions);
  const fetchSession = useSessionsStore((s) => s.fetchSession);
  const currentSessionRef = useRef<string | null>(null);
  const isStreamingRef = useRef(false);

  useEffect(() => {
    // Seed from current state: zustand's subscribe doesn't fire immediately, so without
    // this a share_revoked/session_deleted arriving before the first change wouldn't
    // match the open session and wouldn't navigate the user out.
    currentSessionRef.current = useSessionsStore.getState().currentSession?.id ?? null;
    isStreamingRef.current = useChatStore.getState().isStreaming;
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
    const reconnectBudget = createReconnectBudget(RECONNECT_BUDGET_MS);
    const ticketAbort = new AbortController();

    // The sync socket is the app-wide heartbeat (mounted once in the shell), so it
    // is the single loop that gates the global give-up: when ITS budget is spent
    // the whole app is degraded → red reload banner. Chat/egress give up quietly.
    function scheduleReconnect() {
      if (unmounted) return;
      if (reconnectBudget.expired()) {
        useBackendStore.getState().markGaveUp();
        return;
      }
      retries++;
      const delay = jitteredBackoff(retries - 1, { baseMs: RECONNECT_DELAY, maxMs: RECONNECT_MAX_DELAY });
      reconnectRef.current = setTimeout(() => void connect(), delay);
    }

    // A backgrounded tab throttles setTimeout, so the wall-clock budget can elapse
    // without any real reconnect attempt. Grant a fresh budget when the tab returns
    // to the foreground so the user isn't met with an instant give-up on a backend
    // that may be perfectly healthy.
    const onVisible = () => {
      if (document.visibilityState === 'visible') reconnectBudget.reset();
    };
    document.addEventListener('visibilitychange', onVisible);

    async function connect() {
      if (unmounted) return;

      // Close any existing connection to prevent orphaned sockets
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }

      // Cookie-authed ticket POST (dev mode returns the dev user); the ticket
      // rides the WS subprotocol since the handshake can't carry the cookie.
      let protocols: string[] = ['inkwest'];
      try {
        const ticket = await fetchWsTicket(ticketAbort.signal);
        if (unmounted) return;
        protocols = ['inkwest', `ticket|${ticket}`];
      } catch {
        if (unmounted) return;
        scheduleReconnect();
        return;
      }
      const ws = new WebSocket(`${WS_URL}/ws/sync`, protocols);
      wsRef.current = ws;

      ws.onopen = () => {
        retries = 0;
        reconnectBudget.reset(); // recovered — grant a fresh give-up budget
        // A live socket is proof the backend is reachable — clear the global banner.
        useBackendStore.getState().reachable();
        // Baseline the build on first connect, compare on every reconnect: a
        // deploy that dropped this socket bumps the backend version, so a
        // differing reading surfaces the reload banner. Best-effort.
        void getVersion()
          .then(({ version, build_timestamp }) =>
            useUpdateStore.getState().recordVersion(version, build_timestamp)
          )
          .catch(() => {});
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
          // A new conversation-share invitation — surface the accept/decline popup.
          if (data.type === 'share_invite') {
            useInvitesStore.getState().addInvite({
              share_id: data.share_id,
              session_id: data.session_id,
              session_title: data.session_title ?? null,
              role: data.role,
              invited_by_display_name: data.invited_by ?? null,
              created_at: new Date().toISOString(),
            });
            return;
          }
          // Access was just revoked — surface a polite notice (with the title).
          if (data.type === 'share_revoked') {
            useInvitesStore.getState().addRevokedNotice({
              sessionId: data.session_id,
              title: data.session_title ?? null,
            });
          }
          fetchSessions();
          // A share was accepted/declined/revoked — resync the popup list and signal
          // an open ShareModal (owner) to reload its participant statuses live.
          if (data.type === 'share_responded' || data.type === 'share_revoked') {
            useInvitesStore.getState().fetchInvites();
            useInvitesStore.getState().bumpSharesVersion();
          }
          const sid = data.session_id;
          // The open conversation went away for this user (deleted, their share was
          // revoked, or they left it from another tab) — navigate out rather than let
          // the chat socket loop on retries. session_left carries no popup (self-action).
          if (
            (data.type === 'session_deleted' ||
              data.type === 'share_revoked' ||
              data.type === 'session_left') &&
            sid === currentSessionRef.current
          ) {
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
          // Session expired/invalid — back to root; AuthGuard re-checks the cookie.
          if (!unmounted) navigate('/');
          return;
        }
        // Code 1000 is an app-initiated clean close (our own ws.close); a real
        // deploy/restart closes with 1012/1006, which DO fall through to reconnect.
        if (event.code === 1000) return;
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
      ticketAbort.abort(); // cancel an in-flight ticket fetch + its retry loop
      document.removeEventListener('visibilitychange', onVisible);
      cleanup();
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [fetchSessions, fetchSession, navigate]);
}
