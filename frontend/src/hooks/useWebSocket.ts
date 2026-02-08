import { useEffect, useRef, useCallback } from 'react';
import { useChatStore } from '@/store/chat';
import { useSessionsStore } from '@/store/sessions';
import { ChatWebSocket } from '@/services/websocket';
import { ChatEvent } from '@/types/events';

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
    };
  }, [sessionId, handleEvent]);

  const sendMessage = useCallback((content: string) => {
    if (wsRef.current) {
      // Add user message to store immediately
      addMessage({
        id: crypto.randomUUID(),
        session_id: sessionId,
        role: 'user',
        content,
        created_at: new Date().toISOString(),
      });

      // Send via WebSocket
      wsRef.current.sendMessage(content);
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
