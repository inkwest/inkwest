import { useEffect, useRef, useState } from 'react';
import { useChatStore } from '@/store/chat';
import { ApiError, getSharedConversation, wsBaseUrl } from '@/services/api';
import { jitteredBackoff, createReconnectBudget, RECONNECT_BUDGET_MS } from '@/lib/backoff';
import { ChatEvent } from '@/types/events';
import { SharedConversation } from '@/types/api';

type Status = 'loading' | 'ready' | 'error' | 'revoked';
// Liveness of the transcript once a snapshot is on screen. 'stale' is terminal —
// the budget is spent or the server is refusing more guests — so what is on screen
// will not update again without a reload.
type Connection = 'live' | 'reconnecting' | 'stale';

// Same backoff band as the other WS loops (see lib/backoff.ts).
const RECONNECT_DELAY = 3000;
const RECONNECT_MAX_DELAY = 30000;
const PING_INTERVAL = 30000;

// Anonymous read-only viewer of a conversation via a share-link token. Reads the
// transcript from the public REST endpoint and feeds the token-authed
// /ws/shared/{token} socket (no OIDC ticket) into the shared chat store, so a guest
// sees turns stream in real time. Read-only: it sends nothing but keepalive pings.
// /shared/:token is outside ShellLayout, so BackendBanner never renders there:
// `connection` is the page's only connectivity signal.
export function useSharedSocket(token: string) {
  const [status, setStatus] = useState<Status>('loading');
  const [connection, setConnection] = useState<Connection>('live');
  const [meta, setMeta] = useState<Pick<SharedConversation, 'title' | 'owner_display_name'> | null>(
    null
  );
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    let retries = 0;
    let loadedOnce = false;
    let pingTimer: ReturnType<typeof setInterval> | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    const reconnectBudget = createReconnectBudget(RECONNECT_BUDGET_MS);
    let readAbort: AbortController | null = null; // non-null while a read is in flight
    let readDirty = false;
    const {
      resetChat,
      setMessages,
      appendStreamingText,
      setActiveToolCall,
      addToolResult,
      clearStreamingMessage,
      clearToolResults,
      clearStreamingBlocks,
      setIsStreaming,
      addMessage,
      addPeerMessage,
    } = useChatStore.getState();

    resetChat();

    function clearStreamingState() {
      clearStreamingMessage();
      clearToolResults();
      clearStreamingBlocks();
      setActiveToolCall(null);
      setIsStreaming(false);
    }

    function handleEvent(event: ChatEvent) {
      switch (event.type) {
        case 'stream_resumed':
          clearStreamingBlocks();
          setIsStreaming(true);
          break;
        case 'text_delta':
          setIsStreaming(true);
          appendStreamingText(event.content || '');
          break;
        case 'tool_start':
          setActiveToolCall({ name: event.tool_name!, input: event.tool_input! });
          break;
        case 'tool_result':
          setActiveToolCall(null);
          addToolResult({
            name: event.tool_name!,
            content: event.content!,
            is_error: event.is_error || false,
          });
          break;
        case 'done': {
          const blocks = useChatStore.getState().streamingBlocks;
          const wasStopped = !!event.metadata?.stopped;
          if (blocks.length > 0) {
            const content = blocks.map((b) =>
              b.type === 'text'
                ? { type: 'text' as const, text: b.content }
                : b.type === 'tool_use'
                  ? { type: 'tool_use' as const, name: b.name, input: b.input }
                  : { type: 'tool_result' as const, content: b.content, is_error: b.is_error }
            );
            if (wasStopped) {
              content.push({ type: 'text', text: '\n\n---\n*Response interrupted by user.*' });
            }
            addMessage({
              id: crypto.randomUUID(),
              session_id: '',
              role: 'assistant',
              content,
              created_at: new Date().toISOString(),
            });
          } else if (wasStopped) {
            addMessage({
              id: crypto.randomUUID(),
              session_id: '',
              role: 'assistant',
              content: [{ type: 'text', text: '*Response interrupted by user.*' }],
              created_at: new Date().toISOString(),
            });
          }
          clearStreamingState();
          // A read issued before this turn ended answers with a transcript that predates
          // it (and any user_message it clobbered): supersede it. Every engine persists
          // the answer before emitting `done`, so the fresh read includes it.
          if (readAbort || readDirty) {
            readDirty = false;
            void loadSnapshot();
          }
          break;
        }
        case 'user_message':
          // Broadcast BEFORE the turn persists it: a read in flight now will overwrite
          // this line, so flag it for the re-read at `done`.
          if (readAbort) readDirty = true;
          addPeerMessage({
            id: crypto.randomUUID(),
            session_id: '',
            role: 'user',
            content: event.content || '',
            attachments: event.attachments?.length ? event.attachments : undefined,
            created_at: new Date().toISOString(),
          });
          break;
        // Presence/typing are intentionally not handled here — the public share view
        // shows no presence (the guest shouldn't see who else is viewing).
      }
    }

    function scheduleReconnect() {
      if (cancelled) return;
      if (reconnectBudget.expired()) {
        // Budget spent. Nothing else will surface this on a shell-less route, so
        // mark the transcript stale and let the page ask for a reload.
        setConnection('stale');
        return;
      }
      setConnection('reconnecting');
      reconnectTimer = setTimeout(
        () => void connect(),
        jitteredBackoff(retries++, { baseMs: RECONNECT_DELAY, maxMs: RECONNECT_MAX_DELAY })
      );
    }

    // A backgrounded tab throttles timers, so the wall-clock budget can elapse with
    // no real attempt behind it. Grant a fresh one when the guest comes back.
    const onVisible = () => {
      if (document.visibilityState === 'visible') reconnectBudget.reset();
    };
    document.addEventListener('visibilitychange', onVisible);

    // Read the transcript from the DB. Only `messages` is replaced: a turn being
    // replayed into the streaming blocks right now must survive the refresh.
    //
    // Each read supersedes the one before it: a read issued by a socket that has
    // since dropped would otherwise answer LAST and write its older transcript over
    // the fresher one the current socket just read.
    async function loadSnapshot(): Promise<'ok' | 'gone' | 'failed'> {
      readAbort?.abort();
      const ctrl = new AbortController();
      readAbort = ctrl;
      try {
        const conv = await getSharedConversation(token, ctrl.signal);
        if (cancelled || ctrl.signal.aborted) return 'failed';
        setMeta({ title: conv.title, owner_display_name: conv.owner_display_name });
        // Strip 'system' lines defensively (the API already excludes them).
        setMessages(conv.messages.filter((m) => m.role !== 'system'));
        setStatus('ready');
        return 'ok';
      } catch (e) {
        if (cancelled || ctrl.signal.aborted) return 'failed';
        // 404 is the terminal answer — the link never existed or was revoked.
        if (e instanceof ApiError && e.status === 404) {
          setStatus('revoked');
          return 'gone';
        }
        return 'failed';
      } finally {
        if (readAbort === ctrl) readAbort = null;
      }
    }

    async function connect() {
      if (cancelled) return;
      if (pingTimer) clearInterval(pingTimer); // never outlive the socket that armed it
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }

      // First paint must work even where WebSockets are blocked (corporate proxies
      // on a guest's network), so the first read happens before the handshake;
      // every later read rides ws.onopen instead.
      if (!loadedOnce) {
        const result = await loadSnapshot();
        if (cancelled || result === 'gone') return;
        if (result === 'failed') {
          // Transient (fetchAPI already exhausted its own retries): keep whatever is
          // on screen and retry the whole sequence.
          setStatus((s) => (s === 'loading' ? 'error' : s));
          scheduleReconnect();
          return;
        }
        loadedOnce = true;
      }

      const ws = new WebSocket(`${wsBaseUrl()}/ws/shared/${token}`, 'inkwest');
      wsRef.current = ws;
      ws.onopen = () => {
        retries = 0;
        reconnectBudget.reset(); // recovered — grant a fresh give-up budget
        setConnection('live');
        pingTimer = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
        }, PING_INTERVAL);
        // Drop the previous socket's partial turn. Frames from this socket only start
        // after onopen: a turn still running comes back via stream_resumed + buffer
        // replay; one that ended while we were down is only in the DB (below).
        clearStreamingState();
        // Read the transcript only now that the socket is registered. The server
        // replays the turn currently in flight and nothing older
        // (chat.py::_follow_turns), so a turn that finished before this socket
        // attached exists only in the DB — reading any earlier leaves that gap
        // permanently, since a healthy socket never reconnects to heal it.
        void loadSnapshot();
      };
      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.type === 'pong') return;
          handleEvent(data as ChatEvent);
        } catch {
          // ignore malformed frame
        }
      };
      ws.onclose = (e) => {
        if (pingTimer) clearInterval(pingTimer);
        if (cancelled) return;
        // 4004 (invalid) / 4012 (revoked while viewing) → the link is no longer valid.
        if (e.code === 4004 || e.code === 4012) {
          setStatus('revoked');
          return;
        }
        // 4009 = this link is at its concurrent-guest cap: the server refuses on
        // purpose, so don't retry; a reload once a slot frees is the way back.
        if (e.code === 4009) {
          setConnection('stale');
          return;
        }
        // Everything else (deploy, gateway idle-close, network blip, sleep) is transient.
        scheduleReconnect();
      };
      ws.onerror = () => ws.close();
    }

    void connect();

    return () => {
      cancelled = true;
      readAbort?.abort(); // cancel an in-flight read + its retry loop
      document.removeEventListener('visibilitychange', onVisible);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      // Clear the ping here too: the cleanup nulls onclose below, so the onclose
      // handler (which also clears it) won't run on the close we trigger.
      if (pingTimer) clearInterval(pingTimer);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
      useChatStore.getState().resetChat();
    };
  }, [token]);

  return { status, connection, meta };
}
