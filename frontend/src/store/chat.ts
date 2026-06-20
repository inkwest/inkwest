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

// Todo item type for tracking task progress
export interface TodoItem {
  content: string;
  status: 'pending' | 'in_progress' | 'completed' | 'interrupted';
  activeForm?: string;
  // Claude Code 2.x assigns each task a sequential id (TaskCreate); TaskUpdate
  // references it. Absent for legacy TodoWrite (which carries the whole list).
  id?: string;
}

// Apply a task-tracking tool call to the running todo list. Supports both
// Claude Code ≤1.x (TodoWrite: one call carries the whole list) and 2.x
// (TaskCreate/TaskUpdate: incremental). The CLI assigns task ids sequentially
// in creation order and monotonically across a session's turns, so a task's
// position (length+1 at creation) reproduces the id TaskUpdate will reference —
// no need to read the create result, keeping this purely frontend.
export function applyTaskTool(
  todos: TodoItem[],
  name: string | undefined,
  input: Record<string, unknown> | undefined
): TodoItem[] {
  if (name === 'TodoWrite' && Array.isArray(input?.todos)) {
    return input.todos as TodoItem[];
  }
  if (name === 'TaskCreate' && typeof input?.subject === 'string') {
    return [
      ...todos,
      {
        id: String(todos.length + 1),
        content: input.subject as string,
        activeForm: input.activeForm as string | undefined,
        status: 'pending',
      },
    ];
  }
  if (name === 'TaskUpdate' && input?.taskId != null) {
    const taskId = String(input.taskId);
    const status = input.status as TodoItem['status'];
    return todos.map((t) => (t.id === taskId ? { ...t, status } : t));
  }
  return todos;
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
  // Current todos from TodoWrite tool
  currentTodos: TodoItem[];
  isStreaming: boolean;
  isConnected: boolean;
  error: string | null;

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
  setError: (error: string | null) => void;
  addMessage: (message: Message) => void;
  // Optimistically drop a failed message's error UI when its turn is retried.
  clearMessageError: (messageId: string) => void;
  // New actions for inline streaming
  clearStreamingBlocks: () => void;
  // Todo actions
  setTodos: (todos: TodoItem[]) => void;
  clearTodos: () => void;
  markTodosInterrupted: () => void;
  // Session lifecycle
  resetChat: () => void;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  streamingMessage: '',
  activeToolCall: null,
  toolResults: [],
  streamingBlocks: [],
  currentTodos: [],
  isStreaming: false,
  isConnected: false,
  error: null,

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
          currentTodos: applyTaskTool(state.currentTodos, tool.name, tool.input),
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
  setError: (error) => set({ error }),
  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),
  clearMessageError: (messageId) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === messageId
          ? { ...m, status: 'success', is_retryable: false, error_message: undefined }
          : m
      ),
    })),
  clearStreamingBlocks: () => set({ streamingBlocks: [] }),
  setTodos: (todos) => set({ currentTodos: todos }),
  clearTodos: () => set({ currentTodos: [] }),
  markTodosInterrupted: () =>
    set((state) => ({
      currentTodos: state.currentTodos.map((todo) =>
        todo.status === 'in_progress' || todo.status === 'pending'
          ? { ...todo, status: 'interrupted' as const }
          : todo
      ),
    })),
  resetChat: () =>
    set({
      messages: [],
      streamingMessage: '',
      activeToolCall: null,
      toolResults: [],
      streamingBlocks: [],
      currentTodos: [],
      isStreaming: false,
      error: null,
    }),
}));

/**
 * Reconstruct the floating task list from a loaded session's messages.
 * Legacy Claude Code (TodoWrite) carries the whole list in the last call, so we
 * return that; Claude Code 2.x emits incremental TaskCreate/TaskUpdate, so we
 * replay them in order. A session uses a single CLI version, so it's either-or.
 */
export function extractTodosFromMessages(messages: Message[]): TodoItem[] {
  // Legacy TodoWrite: newest call holds the full, current list.
  for (let i = messages.length - 1; i >= 0; i--) {
    const content = messages[i].content;
    if (!Array.isArray(content)) continue;
    for (let j = content.length - 1; j >= 0; j--) {
      const block = content[j];
      if (block.type === 'tool_use' && block.name === 'TodoWrite' && Array.isArray(block.input?.todos)) {
        return block.input.todos as TodoItem[];
      }
    }
  }
  // Claude Code 2.x: replay TaskCreate/TaskUpdate chronologically.
  let todos: TodoItem[] = [];
  for (const message of messages) {
    const content = message.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block.type === 'tool_use') {
        todos = applyTaskTool(todos, block.name, block.input);
      }
    }
  }
  return todos;
}
