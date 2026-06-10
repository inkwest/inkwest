import { create } from 'zustand';
import { Message } from '@/types/api';

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
    set((state) =>
      state.isStreaming && messages.length < state.messages.length
        ? state
        : { messages }
    ),
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
