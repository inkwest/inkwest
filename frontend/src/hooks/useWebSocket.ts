import { useEffect, useRef, useCallback } from 'react';
import { useChatStore } from '@/store/chat';
import { useSessionsStore } from '@/store/sessions';
import { ChatWebSocket } from '@/services/websocket';
import { ChatEvent } from '@/types/events';
import { Attachment } from '@/types/api';
import { useConnectorsStore } from '@/store/connectors';
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
    setIsConnected,
    setError,
    addMessage,
    markTodosInterrupted,
  } = useChatStore();

  const handleEvent = useCallback((event: ChatEvent) => {
    switch (event.type) {
      case 'connected' as unknown as ChatEvent['type']:
        setIsConnected(true);
        setError(null);
        break;

      case 'disconnected' as unknown as ChatEvent['type']:
        setIsConnected(false);
        break;

      case 'stream_resumed':
        // Reconnected to a turn still running on the server (we navigated back).
        // Clear any stale streaming state, then the replayed buffer rebuilds it.
        clearStreamingBlocks();
        setIsStreaming(true);
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

        if (wasStopped) {
          markTodosInterrupted();
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
        break;
      }

      case 'error':
        setError(event.content || 'An error occurred');
        setIsStreaming(false);
        setActiveToolCall(null);
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
    setIsConnected,
    setError,
    addMessage,
    markTodosInterrupted,
  ]);

  useEffect(() => {
    // A previous socket's stale isConnected=true would let ChatInput's autoSubmit
    // fire before this socket is open, and send() drops messages silently then.
    setIsConnected(false);

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
    };
  }, [sessionId, handleEvent, setIsConnected]);

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

      // Send via WebSocket
      wsRef.current.sendMessage(content, attachments);
      setIsStreaming(true);
    }
  }, [sessionId, addMessage, setIsStreaming]);

  const stopStreaming = useCallback(() => {
    wsRef.current?.sendStop();
  }, []);

  return {
    sendMessage,
    stopStreaming,
    isConnected: useChatStore((s) => s.isConnected),
    isStreaming: useChatStore((s) => s.isStreaming),
    error: useChatStore((s) => s.error),
  };
}
