import { useEffect, useRef, useCallback } from 'react';
import { useChatStore } from '@/store/chat';
import { useSessionsStore } from '@/store/sessions';
import { ChatWebSocket } from '@/services/websocket';
import { ChatEvent } from '@/types/events';
import { Attachment } from '@/types/api';
import { useConnectorsStore } from '@/store/connectors';
import { useMcpStore } from '@/store/mcp';
import { ConnectorName } from '@/types/connectors';

export function useWebSocket(sessionId: string) {
  const wsRef = useRef<ChatWebSocket | null>(null);

  const {
    appendStreamingText,
    clearStreamingMessage,
    setActiveToolCall,
    addToolResult,
    clearToolResults,
    clearStreamingBlocks,
    setIsStreaming,
    setResumingTurn,
    dropInProgressAssistant,
    setIsConnected,
    setIsReconnecting,
    setError,
    addMessage,
    addPeerMessage,
    clearMessageError,
    setPresence,
    setTyping,
    clearPresence,
  } = useChatStore();

  const handleEvent = useCallback((event: ChatEvent) => {
    // Once the current turn ends, send everything the user queued during it as a
    // single combined turn (the backend rejects concurrent turns, so we can't send
    // them mid-stream). Runs on both `done` and `error` — the user opted to flush
    // even after a stop/error. No-op when the queue is empty.
    const flushQueue = () => {
      const { queuedMessages, clearQueue } = useChatStore.getState();
      if (queuedMessages.length === 0) return;
      // Skip blank contents (an attachment-only or edited-empty entry) so the join
      // doesn't open with a stray blank line.
      const content = queuedMessages.map((m) => m.content.trim()).filter(Boolean).join('\n\n');
      const attachments = queuedMessages.flatMap((m) => m.attachments ?? []);
      clearQueue();
      setError(null); // moving on to a fresh turn — drop any just-shown error banner
      addMessage({
        id: crypto.randomUUID(),
        session_id: sessionId,
        role: 'user',
        content,
        attachments: attachments.length ? attachments : undefined,
        created_at: new Date().toISOString(),
      });
      wsRef.current?.sendMessage(content, attachments.length ? attachments : undefined);
      setIsStreaming(true);
    };

    switch (event.type) {
      case 'connected':
        setIsConnected(true);
        setIsReconnecting(false);
        setError(null);
        break;

      case 'disconnected':
        setIsConnected(false);
        // Drop the stale roster/typing so a departed peer doesn't linger as
        // "viewing"/"typing…" until (if ever) a fresh frame arrives.
        clearPresence();
        // Clear the resume flag: a reconnect re-sets it via stream_resumed if the turn is still
        // running, so it never leaks past a turn that ends while we're disconnected.
        setResumingTurn(false);
        break;

      case 'reconnecting':
        // The socket dropped and the service is retrying (state:true) or has given
        // up (state:false). Drives the "Reconnecting…" banner/badge over the bare
        // "Disconnected" state.
        setIsReconnecting(!!event.state);
        break;

      case 'stream_resumed':
        // Reconnected to a turn still running on the server (we navigated back).
        // Clear any stale streaming state, then the replayed buffer rebuilds it. Mark the turn
        // as resuming: its persisted in-progress copy is in `messages` (session load) and will
        // be replayed — transcriptForRender hides it live, and `done` drops it so the completed
        // turn isn't a duplicate.
        clearStreamingBlocks();
        setIsStreaming(true);
        setResumingTurn(true);
        break;

      case 'text_delta':
        setIsStreaming(true);
        appendStreamingText(event.content || '');
        break;

      case 'tool_start':
        setActiveToolCall({
          name: event.tool_name!,
          input: event.tool_input!,
        });
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
        const store = useChatStore.getState();
        const wasStopped = !!event.metadata?.stopped;

        // Resumed turn: drop its stale persisted in-progress copy before appending the
        // completed turn below, else the reload would leave two identical assistant bubbles.
        if (store.resumingTurn) {
          dropInProgressAssistant();
          setResumingTurn(false);
        }

        if (store.streamingBlocks.length > 0) {
          const content: Array<
            | { type: 'text'; text: string }
            | { type: 'tool_use'; name: string; input: Record<string, unknown> }
            | { type: 'tool_result'; content: string; is_error: boolean }
          > = [];

          store.streamingBlocks.forEach((block) => {
            if (block.type === 'text') {
              content.push({ type: 'text', text: block.content });
            } else if (block.type === 'tool_use') {
              content.push({
                type: 'tool_use',
                name: block.name,
                input: block.input,
              });
            } else if (block.type === 'tool_result') {
              content.push({
                type: 'tool_result',
                content: block.content,
                is_error: block.is_error,
              });
            }
          });

          if (wasStopped) {
            content.push({ type: 'text', text: '\n\n---\n*Response interrupted by user.*' });
          }

          addMessage({
            id: crypto.randomUUID(),
            session_id: sessionId,
            role: 'assistant',
            content,
            created_at: new Date().toISOString(),
          });
        } else if (wasStopped) {
          addMessage({
            id: crypto.randomUUID(),
            session_id: sessionId,
            role: 'assistant',
            content: [{ type: 'text', text: '*Response interrupted by user.*' }],
            created_at: new Date().toISOString(),
          });
        }

        if (event.metadata) {
          const inp = event.metadata.input_tokens as number | undefined;
          const out = event.metadata.output_tokens as number | undefined;
          if (inp || out) {
            useSessionsStore.getState().updateSessionTokens(sessionId, inp || 0, out || 0);
          }
        }

        clearStreamingMessage();
        clearToolResults();
        clearStreamingBlocks();
        setActiveToolCall(null);
        setIsStreaming(false);
        flushQueue();
        break;
      }

      case 'error':
        setError(event.content || 'An error occurred');
        setIsStreaming(false);
        setResumingTurn(false);
        setActiveToolCall(null);
        flushQueue();
        break;

      case 'connector_required': {
        // A tool needs a connector that isn't granted/expired — surface a
        // re-auth affordance in the connector panel and refresh its status.
        const connector = event.metadata?.connector as ConnectorName | undefined;
        if (connector) {
          useConnectorsStore.getState().setReauthRequired(connector);
          useConnectorsStore.getState().fetchStatus(sessionId);
        }
        break;
      }

      case 'mcp_auth_required': {
        // An enabled OAuth2 MCP server has no active grant — surface a reconnect banner.
        const mcpKey = event.metadata?.mcp_key as string | undefined;
        if (mcpKey) useMcpStore.getState().flagAuthRequired(mcpKey);
        break;
      }

      case 'mcp_tool_ask':
        // The proxy is holding a tool call on this user's decision — stack an ask banner.
        if (event.ask_id && event.mcp_key && event.tool) {
          useMcpStore.getState().addAsk({
            askId: event.ask_id,
            mcpKey: event.mcp_key,
            tool: event.tool,
            // '' (frame from a pre-session_id backend) hides the conversation scope
            // in the decision bar rather than misrouting the write.
            sessionId: event.session_id ?? '',
          });
        }
        break;

      case 'mcp_tool_ask_resolved':
        // Decided on another tab, timed out, or denied for lack of a client — clear it.
        if (event.ask_id) useMcpStore.getState().removeAsk(event.ask_id);
        break;

      case 'presence':
        setPresence(event.users || []);
        break;

      case 'typing':
        if (event.user_id) setTyping(event.user_id, event.display_name || '', !!event.state);
        break;

      case 'user_message':
        // A peer (the owner) sent a turn — show it live for read-only viewers. The
        // owner's own client doesn't receive this (the server excludes the sender).
        addPeerMessage({
          id: crypto.randomUUID(),
          session_id: sessionId,
          role: 'user',
          content: event.content || '',
          attachments: event.attachments?.length ? event.attachments : undefined,
          created_at: new Date().toISOString(),
        });
        break;

      case 'pong':
        // Keepalive response, no action needed
        break;
    }
  }, [
    sessionId,
    appendStreamingText,
    clearStreamingMessage,
    setActiveToolCall,
    addToolResult,
    clearToolResults,
    clearStreamingBlocks,
    setIsStreaming,
    setResumingTurn,
    dropInProgressAssistant,
    setIsConnected,
    setIsReconnecting,
    setError,
    addMessage,
    addPeerMessage,
    setPresence,
    setTyping,
    clearPresence,
  ]);

  useEffect(() => {
    // A previous socket's stale isConnected=true would let ChatInput's autoSubmit
    // fire before this socket is open, and send() drops messages silently then.
    setIsConnected(false);
    setIsReconnecting(false);

    // Create and connect WebSocket
    const ws = new ChatWebSocket(sessionId);
    wsRef.current = ws;

    // Subscribe to events
    const unsubscribe = ws.subscribe(handleEvent);

    // Connect
    void ws.connect();

    // Cleanup on unmount
    return () => {
      unsubscribe();
      ws.disconnect();
      // The 'disconnected' event lands after unsubscribe, so reset it here.
      setIsConnected(false);
      setIsReconnecting(false);
    };
  }, [sessionId, handleEvent, setIsConnected, setIsReconnecting]);

  const sendMessage = useCallback((content: string, attachments?: Attachment[]) => {
    if (wsRef.current) {
      // Add user message to store immediately (with its attachments, so the
      // thumbnails show right away — before the server snapshot reconciles it).
      addMessage({
        id: crypto.randomUUID(),
        session_id: sessionId,
        role: 'user',
        content,
        attachments: attachments?.length ? attachments : undefined,
        created_at: new Date().toISOString(),
      });

      // Surface a brand-new conversation in the sidebar the instant its first message
      // goes out — the sidebar hides untitled sessions, and the server's title NOTIFY
      // can be missed if the sync socket isn't subscribed yet. No-op once titled.
      useSessionsStore.getState().ensureSessionTitle(sessionId, content);

      // Send via WebSocket
      wsRef.current.sendMessage(content, attachments);
      setIsStreaming(true);
    }
  }, [sessionId, addMessage, setIsStreaming]);

  const stopStreaming = useCallback(() => {
    wsRef.current?.sendStop();
  }, []);

  const sendTyping = useCallback((state: boolean) => {
    wsRef.current?.sendTyping(state);
  }, []);

  // Re-run an interrupted turn. The failed user message already exists (server-side
  // and in the store), so we don't add a new bubble — we optimistically drop its
  // error UI and let the backend replay it in place. Only mutate the UI if the
  // frame actually went out: on a closed/reconnecting socket retry is dropped (not
  // queued), so faking success would strand the view in a perpetual streaming state.
  // If the server then rejects the retry (e.g. a turn is already running), the
  // error event resets streaming; the bubble's button stays hidden until the next
  // refetch — an accepted, self-healing cosmetic gap in favor of a snappy click.
  const retryMessage = useCallback((messageId: string) => {
    if (!wsRef.current?.sendRetry()) return;
    clearMessageError(messageId);
    setIsStreaming(true);
  }, [clearMessageError, setIsStreaming]);

  return {
    sendMessage,
    stopStreaming,
    sendTyping,
    retryMessage,
    isConnected: useChatStore((s) => s.isConnected),
    isStreaming: useChatStore((s) => s.isStreaming),
    error: useChatStore((s) => s.error),
  };
}
