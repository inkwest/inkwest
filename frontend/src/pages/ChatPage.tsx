import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useSessionsStore } from '@/store/sessions';
import { useChatStore } from '@/store/chat';
import { ChatView } from '@/components/chat/ChatView';
import { useWebSocket } from '@/hooks/useWebSocket';
import { Attachment } from '@/types/api';

export function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { currentSession, fetchSession, isLoadingSession, consumePendingPrompt, consumePendingAttachments } =
    useSessionsStore();
  const { applyServerMessages, resetChat } = useChatStore();
  // A prompt (+ any attachments) handed off from the start screen. It seeds the
  // composer (so it stays visible/recoverable if the WS never connects) and is
  // auto-submitted once connected — see ChatInput's autoSubmit.
  const [initialPrompt, setInitialPrompt] = useState<string | null>(null);
  const [initialAttachments, setInitialAttachments] = useState<Attachment[] | null>(null);

  useEffect(() => {
    const prompt = consumePendingPrompt();
    if (prompt) setInitialPrompt(prompt);
    const attachments = consumePendingAttachments();
    if (attachments) setInitialAttachments(attachments);
  }, [consumePendingPrompt, consumePendingAttachments]);

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
      applyServerMessages(serverMessages);
    }
  }, [serverMessages, applyServerMessages]);

  const { sendMessage, stopStreaming, retryMessage } = useWebSocket(sessionId!);

  const handleSend = (content: string, attachments?: Attachment[]) => {
    sendMessage(content, attachments);
    setInitialPrompt(null);
    setInitialAttachments(null);
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
      onRetryMessage={retryMessage}
      initialPrompt={initialPrompt}
      initialAttachments={initialAttachments}
      autoSubmit
      sessionId={sessionId}
    />
  );
}
