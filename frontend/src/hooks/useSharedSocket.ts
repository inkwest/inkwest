import { useEffect, useRef, useState } from 'react';
import { useChatStore } from '@/store/chat';
import { getSharedConversation, wsBaseUrl } from '@/services/api';
import { ChatEvent } from '@/types/events';
import { SharedConversation } from '@/types/api';

type Status = 'loading' | 'ready' | 'error' | 'revoked';

// Anonymous read-only viewer of a conversation via a share-link token. Loads the
// transcript once over the public REST endpoint, then opens the token-authed
// /ws/shared/{token} socket (no OIDC ticket) and feeds its live events into the
// shared chat store — so a guest sees turns stream and presence update in real time.
// Read-only: it never sends anything but keepalive pings.
export function useSharedSocket(token: string) {
  const [status, setStatus] = useState<Status>('loading');
  const [meta, setMeta] = useState<Pick<SharedConversation, 'title' | 'owner_display_name'> | null>(
    null
  );
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    let pingTimer: ReturnType<typeof setInterval> | undefined;
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
          clearStreamingMessage();
          clearToolResults();
          clearStreamingBlocks();
          setActiveToolCall(null);
          setIsStreaming(false);
          break;
        }
        case 'user_message':
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

    async function start() {
      try {
        const conv = await getSharedConversation(token);
        if (cancelled) return;
        setMeta({ title: conv.title, owner_display_name: conv.owner_display_name });
        // Strip 'system' lines defensively (the API already excludes them).
        setMessages(conv.messages.filter((m) => m.role !== 'system'));
        setStatus('ready');
      } catch {
        if (!cancelled) setStatus('error');
        return;
      }

      const ws = new WebSocket(`${wsBaseUrl()}/ws/shared/${token}`, 'inkwest');
      wsRef.current = ws;
      ws.onopen = () => {
        pingTimer = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
        }, 30000);
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
        // 4004 (invalid) / 4012 (revoked while viewing) → the link is no longer valid.
        if (!cancelled && (e.code === 4004 || e.code === 4012)) setStatus('revoked');
      };
    }

    void start();

    return () => {
      cancelled = true;
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

  return { status, meta };
}
