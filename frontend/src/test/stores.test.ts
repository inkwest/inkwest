import { describe, it, expect, beforeEach } from 'vitest'
import { useSessionsStore } from '../store/sessions'
import { useClustersStore } from '../store/clusters'
import { useChatStore } from '../store/chat'
import { useSettingsStore } from '../store/settings'

describe('sessionsStore', () => {
  beforeEach(() => {
    useSessionsStore.setState({
      sessions: [],
      currentSession: null,
      isLoading: false,
      error: null,
    })
  })

  it('should start with empty sessions', () => {
    const state = useSessionsStore.getState()
    expect(state.sessions).toEqual([])
    expect(state.currentSession).toBeNull()
  })

  it('should set current session', () => {
    const session = {
      id: '123',
      cluster_id: 'cluster-1',
      title: 'Test Session',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      messages: [],
      cluster: {
        id: 'cluster-1',
        name: 'test-cluster',
        display_name: 'Test Cluster',
        is_active: true,
        created_at: new Date().toISOString(),
      },
    }
    useSessionsStore.getState().setCurrentSession(session)
    expect(useSessionsStore.getState().currentSession).toEqual(session)
  })

  it('should clear current session', () => {
    useSessionsStore.getState().setCurrentSession(null)
    expect(useSessionsStore.getState().currentSession).toBeNull()
  })
})

describe('clustersStore', () => {
  beforeEach(() => {
    useClustersStore.setState({
      clusters: [],
      isLoading: false,
      error: null,
    })
  })

  it('should start with empty clusters', () => {
    const state = useClustersStore.getState()
    expect(state.clusters).toEqual([])
  })

  it('should have loading state false initially', () => {
    expect(useClustersStore.getState().isLoading).toBe(false)
  })

  it('should have no error initially', () => {
    expect(useClustersStore.getState().error).toBeNull()
  })
})

describe('chatStore', () => {
  beforeEach(() => {
    useChatStore.setState({
      messages: [],
      streamingMessage: '',
      activeToolCall: null,
      toolResults: [],
      streamingBlocks: [],
      isStreaming: false,
      isConnected: false,
      error: null,
    })
  })

  it('should start with empty state', () => {
    const state = useChatStore.getState()
    expect(state.messages).toEqual([])
    expect(state.streamingBlocks).toEqual([])
    expect(state.isStreaming).toBe(false)
  })

  it('should append streaming text to new block', () => {
    useChatStore.getState().appendStreamingText('Hello ')
    const state = useChatStore.getState()
    expect(state.streamingMessage).toBe('Hello ')
    expect(state.streamingBlocks).toHaveLength(1)
    expect(state.streamingBlocks[0]).toEqual({ type: 'text', content: 'Hello ' })
  })

  it('should append streaming text to existing text block', () => {
    useChatStore.getState().appendStreamingText('Hello ')
    useChatStore.getState().appendStreamingText('World')
    const state = useChatStore.getState()
    expect(state.streamingMessage).toBe('Hello World')
    expect(state.streamingBlocks).toHaveLength(1)
    expect(state.streamingBlocks[0]).toEqual({ type: 'text', content: 'Hello World' })
  })

  it('should add tool_use block when setActiveToolCall is called', () => {
    useChatStore.getState().appendStreamingText('Let me check.')
    useChatStore.getState().setActiveToolCall({
      name: 'Bash',
      input: { command: 'kubectl get pods', description: 'List pods' },
    })
    const state = useChatStore.getState()
    expect(state.streamingBlocks).toHaveLength(2)
    expect(state.streamingBlocks[0].type).toBe('text')
    expect(state.streamingBlocks[1]).toEqual({
      type: 'tool_use',
      name: 'Bash',
      input: { command: 'kubectl get pods', description: 'List pods' },
      isLoading: true,
    })
  })

  it('should add tool_result block and mark tool_use as not loading', () => {
    useChatStore.getState().setActiveToolCall({
      name: 'Bash',
      input: { command: 'kubectl get pods' },
    })
    useChatStore.getState().addToolResult({
      name: 'Bash',
      content: 'NAME    READY\nnginx   1/1',
      is_error: false,
    })
    const state = useChatStore.getState()
    expect(state.streamingBlocks).toHaveLength(2)
    // First block (tool_use) should have isLoading: false
    const toolUse = state.streamingBlocks[0]
    if (toolUse.type === 'tool_use') {
      expect(toolUse.isLoading).toBe(false)
    }
    // Second block should be tool_result
    expect(state.streamingBlocks[1]).toEqual({
      type: 'tool_result',
      name: 'Bash',
      content: 'NAME    READY\nnginx   1/1',
      is_error: false,
    })
  })

  it('should create new text block after tool blocks', () => {
    useChatStore.getState().appendStreamingText('Checking...')
    useChatStore.getState().setActiveToolCall({ name: 'Bash', input: {} })
    useChatStore.getState().addToolResult({ name: 'Bash', content: 'result', is_error: false })
    useChatStore.getState().appendStreamingText('Done!')

    const state = useChatStore.getState()
    expect(state.streamingBlocks).toHaveLength(4)
    expect(state.streamingBlocks[0].type).toBe('text')
    expect(state.streamingBlocks[1].type).toBe('tool_use')
    expect(state.streamingBlocks[2].type).toBe('tool_result')
    expect(state.streamingBlocks[3].type).toBe('text')
  })

  it('should clear all streaming state', () => {
    useChatStore.getState().appendStreamingText('Hello')
    useChatStore.getState().setActiveToolCall({ name: 'Bash', input: {} })

    useChatStore.getState().clearStreamingMessage()
    useChatStore.getState().clearToolResults()
    useChatStore.getState().clearStreamingBlocks()

    const state = useChatStore.getState()
    expect(state.streamingMessage).toBe('')
    expect(state.toolResults).toEqual([])
    expect(state.streamingBlocks).toEqual([])
  })
})

describe('chatStore todos', () => {
  beforeEach(() => {
    useChatStore.setState({
      currentTodos: [],
      streamingBlocks: [],
      activeToolCall: null,
    })
  })

  it('should start with empty todos', () => {
    expect(useChatStore.getState().currentTodos).toEqual([])
  })

  it('should extract todos from TodoWrite tool calls', () => {
    const todos = [
      { content: 'Task 1', status: 'pending' as const, activeForm: 'Doing task 1' },
      { content: 'Task 2', status: 'in_progress' as const, activeForm: 'Doing task 2' },
    ]

    useChatStore.getState().setActiveToolCall({
      name: 'TodoWrite',
      input: { todos },
    })

    expect(useChatStore.getState().currentTodos).toEqual(todos)
  })

  it('should not update todos for non-TodoWrite tools', () => {
    useChatStore.getState().setActiveToolCall({
      name: 'Bash',
      input: { command: 'ls' },
    })

    expect(useChatStore.getState().currentTodos).toEqual([])
  })

  it('should set and clear todos directly', () => {
    const todos = [
      { content: 'Direct task', status: 'completed' as const, activeForm: 'Task' },
    ]

    useChatStore.getState().setTodos(todos)
    expect(useChatStore.getState().currentTodos).toEqual(todos)

    useChatStore.getState().clearTodos()
    expect(useChatStore.getState().currentTodos).toEqual([])
  })

  // Claude Code 2.x emits TaskCreate/TaskUpdate instead of TodoWrite.
  it('should append a task on TaskCreate with a sequential id', () => {
    const { setActiveToolCall } = useChatStore.getState()
    setActiveToolCall({
      name: 'TaskCreate',
      input: { subject: 'Check nodes', description: '…', activeForm: 'Checking nodes' },
    })
    setActiveToolCall({ name: 'TaskCreate', input: { subject: 'Check pods' } })

    expect(useChatStore.getState().currentTodos).toEqual([
      { id: '1', content: 'Check nodes', activeForm: 'Checking nodes', status: 'pending' },
      { id: '2', content: 'Check pods', activeForm: undefined, status: 'pending' },
    ])
  })

  it('should update a task status by taskId on TaskUpdate', () => {
    const { setActiveToolCall } = useChatStore.getState()
    setActiveToolCall({ name: 'TaskCreate', input: { subject: 'A' } })
    setActiveToolCall({ name: 'TaskCreate', input: { subject: 'B' } })
    setActiveToolCall({ name: 'TaskUpdate', input: { taskId: '2', status: 'in_progress' } })
    setActiveToolCall({ name: 'TaskUpdate', input: { taskId: '1', status: 'completed' } })

    expect(useChatStore.getState().currentTodos).toEqual([
      { id: '1', content: 'A', activeForm: undefined, status: 'completed' },
      { id: '2', content: 'B', activeForm: undefined, status: 'in_progress' },
    ])
  })
})

describe('settingsStore', () => {
  beforeEach(() => {
    useSettingsStore.setState({
      compactToolCards: true,
      showTimestamps: false,
    })
  })

  it('should start with compact mode enabled by default', () => {
    const state = useSettingsStore.getState()
    expect(state.compactToolCards).toBe(true)
  })

  it('should set compact mode directly', () => {
    useSettingsStore.getState().setCompactToolCards(true)
    expect(useSettingsStore.getState().compactToolCards).toBe(true)

    useSettingsStore.getState().setCompactToolCards(false)
    expect(useSettingsStore.getState().compactToolCards).toBe(false)
  })

  it('should toggle compact mode', () => {
    expect(useSettingsStore.getState().compactToolCards).toBe(true)

    useSettingsStore.getState().toggleCompactToolCards()
    expect(useSettingsStore.getState().compactToolCards).toBe(false)

    useSettingsStore.getState().toggleCompactToolCards()
    expect(useSettingsStore.getState().compactToolCards).toBe(true)
  })

  it('should hide timestamps by default', () => {
    expect(useSettingsStore.getState().showTimestamps).toBe(false)
  })

  it('should set showTimestamps directly', () => {
    useSettingsStore.getState().setShowTimestamps(true)
    expect(useSettingsStore.getState().showTimestamps).toBe(true)

    useSettingsStore.getState().setShowTimestamps(false)
    expect(useSettingsStore.getState().showTimestamps).toBe(false)
  })

  it('should toggle showTimestamps', () => {
    expect(useSettingsStore.getState().showTimestamps).toBe(false)

    useSettingsStore.getState().toggleShowTimestamps()
    expect(useSettingsStore.getState().showTimestamps).toBe(true)

    useSettingsStore.getState().toggleShowTimestamps()
    expect(useSettingsStore.getState().showTimestamps).toBe(false)
  })

  // Guards the persisted preference: showTimestamps must survive a reload via partialize.
  it('should persist showTimestamps to storage', () => {
    useSettingsStore.getState().setShowTimestamps(true)
    const persisted = JSON.parse(localStorage.getItem('inkwest-settings') ?? '{}')
    expect(persisted.state.showTimestamps).toBe(true)
  })
})
