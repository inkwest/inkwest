import { create } from 'zustand';
import { ContentBlock, Message, MessageContent } from '@/types/api';

// The backend persists a turn the way the model API frames it — and how many rows
// that takes depends on the orchestrator. The CLI path writes ONE fused assistant
// row per turn; the Converse / Anthropic / inkwest-agent paths drive the tool loop
// themselves and write one assistant row + one tool_result row PER ROUND. The live
// stream always fuses a whole turn into a single bubble, so a reloaded multi-round
// turn would fragment into many bubbles where the live one (and the CLI path)
// showed one. Those per-round rows are the API-correct shape for replay (a
// tool_result must be its own row), so they can't be fused server-side — instead
// fuse for presentation here: collapse each run of consecutive assistant +
// tool_result rows into one assistant message, concatenating their blocks in turn
// order. Only `user` rows carry error/retry state (see messages.py) and they break
// the run, so no error affordance is lost. The first row's identity (id, model,
// created_at) leads the fused bubble.
export function groupServerMessages(messages: Message[]): Message[] {
  const grouped: Message[] = [];
  for (const msg of messages) {
    const prev = grouped[grouped.length - 1];
    const partOfAssistantTurn = msg.role === 'assistant' || msg.role === 'tool_result';
    if (partOfAssistantTurn && prev?.role === 'assistant') {
      grouped[grouped.length - 1] = {
        ...prev,
        content: [...toBlocks(prev.content), ...toBlocks(msg.content)],
        // Track absorbed row ids so a search deep-link to a fused-away round
        // still resolves its scroll/highlight target (see MessageBubble anchors).
        mergedIds: [...(prev.mergedIds ?? []), msg.id],
      };
      continue;
    }
    grouped.push(msg);
  }
  return grouped;
}

// Normalize a message's content to a block list so consecutive turn rows can be
// concatenated. Assistant/tool_result rows are block arrays (or a bare string for
// a text-only round); session-event objects never reach here (they are 'system').
function toBlocks(content: MessageContent): ContentBlock[] {
  if (Array.isArray(content)) return content;
  if (typeof content === 'string') return [{ type: 'text', text: content }];
  return [];
}

// Content block types for streaming
type StreamingBlock =
  | { type: 'text'; content: string }
  | { type: 'tool_use'; name: string; input: Record<string, unknown>; isLoading: boolean }
  | { type: 'tool_result'; name: string; content: string; is_error: boolean };

interface ChatState {
  messages: Message[];
  // Legacy fields for backwards compatibility
  streamingMessage: string;
  activeToolCall: {
    name: string;
    input: Record<string, unknown>;
  } | null;
  toolResults: Array<{
    name: string;
    content: string;
    is_error: boolean;
  }>;
  // New: ordered streaming blocks for inline display
  streamingBlocks: StreamingBlock[];
  isStreaming: boolean;
  isConnected: boolean;
  // A dropped socket is auto-retrying (exponential backoff) — distinct from a
  // terminal disconnect so the UI can show "Reconnecting…" rather than a flat
  // "Disconnected" during a transient blip.
  isReconnecting: boolean;
  error: string | null;
  // Collaboration: who is currently viewing this conversation, and who is typing.
  presence: Array<{ user_id: string; display_name: string }>;
  typingUsers: Array<{ user_id: string; display_name: string }>;

  // Actions
  setMessages: (messages: Message[]) => void;
  applyServerMessages: (messages: Message[]) => void;
  appendStreamingText: (text: string) => void;
  clearStreamingMessage: () => void;
  setActiveToolCall: (tool: { name: string; input: Record<string, unknown> } | null) => void;
  addToolResult: (result: { name: string; content: string; is_error: boolean }) => void;
  clearToolResults: () => void;
  setIsStreaming: (streaming: boolean) => void;
  setIsConnected: (connected: boolean) => void;
  setIsReconnecting: (reconnecting: boolean) => void;
  setError: (error: string | null) => void;
  addMessage: (message: Message) => void;
  // Append a peer's (the owner's) message echoed live to read-only viewers. The id is
  // client-generated (the wire frame carries no server id), so the guard only skips an
  // identical re-add, NOT a later server snapshot — that's fine because
  // applyServerMessages replaces the list wholesale and drops this optimistic row.
  addPeerMessage: (message: Message) => void;
  // Optimistically drop a failed message's error UI when its turn is retried.
  clearMessageError: (messageId: string) => void;
  // Collaboration actions
  setPresence: (presence: Array<{ user_id: string; display_name: string }>) => void;
  setTyping: (user_id: string, display_name: string, state: boolean) => void;
  // Drop the live roster + typing on disconnect (a stale "X typing…" would otherwise
  // stick, since the peer's typing:false / departure frame never arrives).
  clearPresence: () => void;
  // New actions for inline streaming
  clearStreamingBlocks: () => void;
  // Session lifecycle
  resetChat: () => void;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  streamingMessage: '',
  activeToolCall: null,
  toolResults: [],
  streamingBlocks: [],
  isStreaming: false,
  isConnected: false,
  isReconnecting: false,
  error: null,
  presence: [],
  typingUsers: [],

  setMessages: (messages) => set({ messages }),
  // Mid-turn, a session refetch (StrictMode double-mount, sync events) can
  // deliver a snapshot taken before the just-sent user message was persisted;
  // blindly applying it would wipe the optimistic bubble while streaming.
  applyServerMessages: (messages) =>
    set((state) => {
      const grouped = groupServerMessages(messages);
      // Compare grouped-to-grouped: state.messages is already grouped, so a stale
      // snapshot is one that would yield fewer turns than we're currently showing.
      return state.isStreaming && grouped.length < state.messages.length
        ? state
        : { messages: grouped };
    }),
  appendStreamingText: (text) =>
    set((state) => {
      // Update legacy field
      const newStreamingMessage = state.streamingMessage + text;

      // Update streaming blocks - append to last text block or create new one
      const blocks = [...state.streamingBlocks];
      const lastBlock = blocks[blocks.length - 1];
      if (lastBlock && lastBlock.type === 'text') {
        blocks[blocks.length - 1] = { ...lastBlock, content: lastBlock.content + text };
      } else {
        blocks.push({ type: 'text', content: text });
      }

      return { streamingMessage: newStreamingMessage, streamingBlocks: blocks };
    }),
  clearStreamingMessage: () => set({ streamingMessage: '' }),
  setActiveToolCall: (tool) =>
    set((state) => {
      if (tool) {
        // Add tool_use block to streaming blocks
        const blocks = [...state.streamingBlocks];
        // Mark any previous loading tool as complete (without result)
        const updatedBlocks = blocks.map((block) =>
          block.type === 'tool_use' && block.isLoading
            ? { ...block, isLoading: false }
            : block
        );
        updatedBlocks.push({
          type: 'tool_use',
          name: tool.name,
          input: tool.input,
          isLoading: true,
        });

        return {
          activeToolCall: tool,
          streamingBlocks: updatedBlocks,
        };
      }
      return { activeToolCall: tool };
    }),
  addToolResult: (result) =>
    set((state) => {
      // Update legacy field
      const newToolResults = [...state.toolResults, result];

      // Add tool_result block and mark corresponding tool_use as not loading
      const blocks = [...state.streamingBlocks].map((block) =>
        block.type === 'tool_use' && block.isLoading
          ? { ...block, isLoading: false }
          : block
      );
      blocks.push({
        type: 'tool_result',
        name: result.name,
        content: result.content,
        is_error: result.is_error,
      });

      return {
        toolResults: newToolResults,
        streamingBlocks: blocks,
        activeToolCall: null,
      };
    }),
  clearToolResults: () => set({ toolResults: [] }),
  setIsStreaming: (streaming) => set({ isStreaming: streaming }),
  setIsConnected: (connected) => set({ isConnected: connected }),
  setIsReconnecting: (reconnecting) => set({ isReconnecting: reconnecting }),
  setError: (error) => set({ error }),
  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),
  addPeerMessage: (message) =>
    set((state) =>
      state.messages.some((m) => m.id === message.id)
        ? state
        : { messages: [...state.messages, message] }
    ),
  clearMessageError: (messageId) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === messageId
          ? { ...m, status: 'success', is_retryable: false, error_message: undefined }
          : m
      ),
    })),
  clearStreamingBlocks: () => set({ streamingBlocks: [] }),
  setPresence: (presence) => set({ presence }),
  setTyping: (user_id, display_name, state) =>
    set((s) => {
      const others = s.typingUsers.filter((u) => u.user_id !== user_id);
      return { typingUsers: state ? [...others, { user_id, display_name }] : others };
    }),
  clearPresence: () => set({ presence: [], typingUsers: [] }),
  resetChat: () =>
    set({
      messages: [],
      streamingMessage: '',
      activeToolCall: null,
      toolResults: [],
      streamingBlocks: [],
      isStreaming: false,
      error: null,
      presence: [],
      typingUsers: [],
    }),
}));
