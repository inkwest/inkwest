import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useSessionsStore } from '@/store/sessions';
import { useChatStore, extractTodosFromMessages } from '@/store/chat';
import { ChatView } from '@/components/chat/ChatView';
import { useWebSocket } from '@/hooks/useWebSocket';

export function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { currentSession, fetchSession, isLoadingSession, consumePendingPrompt } = useSessionsStore();
  const { setMessages, setTodos, resetChat } = useChatStore();
  // A prompt handed off from the start screen. It seeds the composer (so it stays
  // visible/recoverable if the WS never connects) and is auto-submitted once
  // connected — see ChatInput's autoSubmit.
  const [initialPrompt, setInitialPrompt] = useState<string | null>(null);

  useEffect(() => {
    const prompt = consumePendingPrompt();
    if (prompt) setInitialPrompt(prompt);
  }, [consumePendingPrompt]);

  const setCurrentSession = useSessionsStore((s) => s.setCurrentSession);

  useEffect(() => {
    resetChat();
    if (sessionId) {
      fetchSession(sessionId);
    }
    return () => {
      setCurrentSession(null);
    };
  }, [sessionId, fetchSession, resetChat, setCurrentSession]);

  const serverMessages = currentSession?.messages;
  useEffect(() => {
    if (serverMessages) {
      setMessages(serverMessages);
      const todos = extractTodosFromMessages(serverMessages);
      if (todos.length > 0) {
        setTodos(todos);
      }
    }
  }, [serverMessages, setMessages, setTodos]);

  const { sendMessage, stopStreaming } = useWebSocket(sessionId!);

  const handleSend = (content: string) => {
    sendMessage(content);
    setInitialPrompt(null);
  };

  if (isLoadingSession || !currentSession || currentSession.id !== sessionId) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-gray-400">Loading session...</div>
      </div>
    );
  }

  return (
    <ChatView
      onSendMessage={handleSend}
      onStopStreaming={stopStreaming}
      initialPrompt={initialPrompt}
      autoSubmit
      sessionId={sessionId}
    />
  );
}
