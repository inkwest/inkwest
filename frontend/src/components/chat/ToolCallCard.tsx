import { useState, useEffect, useRef } from 'react';
import { ChevronDown, ChevronRight, Terminal, Copy, Check, ListTodo, Circle, CheckCircle2, Loader2 } from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { cn, TEXT_SIZE } from '@/lib/utils';
import { useSettingsStore } from '@/store/settings';

interface TodoItem {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  activeForm?: string;
}

interface Props {
  name: string;
  input?: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  isLoading?: boolean;
  autoCollapse?: boolean;
}

export function ToolCallCard({ name, input, result, isError, isLoading, autoCollapse = true }: Props) {
  // Get compact mode from settings store
  const compact = useSettingsStore((s) => s.compactToolCards);
  // Start expanded only if loading (active tool), otherwise collapsed
  const [expanded, setExpanded] = useState(isLoading === true);
  const [copied, setCopied] = useState(false);
  const wasLoadingRef = useRef(isLoading);

  // Auto-expand when loading starts, auto-collapse when loading finishes or result arrives
  useEffect(() => {
    if (isLoading) {
      setExpanded(true);
      wasLoadingRef.current = true;
    } else if (autoCollapse) {
      // Collapse when loading finishes (was loading, now not) or when result arrives
      if (wasLoadingRef.current || result) {
        setExpanded(false);
        wasLoadingRef.current = false;
      }
    }
  }, [result, isLoading, autoCollapse]);

  // Extract description and command from input for Bash tools
  const description = input?.description as string | undefined;
  const command = input?.command as string | undefined;

  // Check if this is a TodoWrite tool
  const isTodoTool = name.toLowerCase().includes('todo') || Array.isArray(input?.todos);
  const todos = (input?.todos as TodoItem[] | undefined) || [];
  const completedCount = todos.filter(t => t.status === 'completed').length;
  const inProgressTodo = todos.find(t => t.status === 'in_progress');

  // Display title based on tool type
  const getDisplayTitle = () => {
    if (isTodoTool && todos.length > 0) {
      if (inProgressTodo) {
        return inProgressTodo.activeForm || inProgressTodo.content;
      }
      return `Tasks completed`;
    }
    return description || name;
  };
  const displayTitle = getDisplayTitle();

  // Todo counter badge
  const todoCounter = isTodoTool && todos.length > 0 ? `${completedCount}/${todos.length}` : null;

  // Icon sizes based on compact mode - even smaller in compact
  const iconSize = compact ? 'h-2.5 w-2.5' : 'h-4 w-4';
  const smallIconSize = compact ? 'h-2 w-2' : 'h-3 w-3';

  // Get icon based on tool type
  const getIcon = () => {
    if (isTodoTool) {
      return <ListTodo className={cn(iconSize, 'text-blue-400 shrink-0')} />;
    }
    return <Terminal className={cn(iconSize, 'text-green-400 shrink-0')} />;
  };

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const textToCopy = command || JSON.stringify(input, null, 2);
    await navigator.clipboard.writeText(textToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className={cn(
      'border rounded-md overflow-hidden',
      compact ? 'my-1' : 'my-2',
      isError ? 'border-red-500/50' : 'border-gray-600'
    )}>
      <button
        onClick={() => setExpanded(!expanded)}
        className={cn(
          'w-full flex items-center bg-gray-800 hover:bg-gray-700 text-left',
          compact ? 'gap-1.5 px-2 py-1' : 'gap-2 px-3 py-2'
        )}
      >
        {expanded ? (
          <ChevronDown className={cn(iconSize, 'shrink-0')} />
        ) : (
          <ChevronRight className={cn(iconSize, 'shrink-0')} />
        )}
        {getIcon()}
        <span className={cn('truncate flex-1', TEXT_SIZE.title(compact))}>{displayTitle}</span>
        {todoCounter && (
          <span className={cn(
            'rounded shrink-0',
            compact ? 'text-[10px] px-1 py-0.5' : 'text-xs px-1.5 py-0.5',
            completedCount === todos.length
              ? 'bg-green-900/50 text-green-400'
              : 'bg-blue-900/50 text-blue-400'
          )}>
            {todoCounter}
          </span>
        )}
        {input && !isTodoTool && (
          <button
            onClick={handleCopy}
            className={cn('hover:bg-gray-600 rounded shrink-0', compact ? 'p-0.5' : 'p-1')}
            title="Copy command"
          >
            {copied ? (
              <Check className={cn(smallIconSize, 'text-green-400')} />
            ) : (
              <Copy className={cn(smallIconSize, 'text-gray-400')} />
            )}
          </button>
        )}
        {isLoading && (
          <span className={cn('text-yellow-400 shrink-0', TEXT_SIZE.label(compact))}>Running...</span>
        )}
        {isError && (
          <span className={cn('text-red-400 shrink-0', TEXT_SIZE.label(compact))}>Error</span>
        )}
        {result && !isError && !isLoading && (
          <span className={cn('text-green-400 shrink-0', TEXT_SIZE.label(compact))}>Done</span>
        )}
      </button>

      {expanded && (
        <div className={cn('bg-gray-900', compact ? 'p-2' : 'p-3')}>
          {/* Render todo list nicely */}
          {isTodoTool && todos.length > 0 && (
            <div className="space-y-0.5">
              {todos.map((todo, idx) => (
                <div
                  key={idx}
                  className={cn(
                    'flex items-center gap-1.5 py-0.5 px-1.5 rounded',
                    TEXT_SIZE.title(compact),
                    todo.status === 'in_progress' && 'bg-blue-900/30',
                    todo.status === 'completed' && 'text-gray-500'
                  )}
                >
                  {todo.status === 'completed' && (
                    <CheckCircle2 className={cn(compact ? 'h-3 w-3' : 'h-4 w-4', 'text-green-500 shrink-0')} />
                  )}
                  {todo.status === 'in_progress' && (
                    <Loader2 className={cn(compact ? 'h-3 w-3' : 'h-4 w-4', 'text-blue-400 shrink-0 animate-spin')} />
                  )}
                  {todo.status === 'pending' && (
                    <Circle className={cn(compact ? 'h-3 w-3' : 'h-4 w-4', 'text-gray-500 shrink-0')} />
                  )}
                  <span className={todo.status === 'completed' ? 'line-through' : ''}>
                    {todo.content}
                  </span>
                </div>
              ))}
            </div>
          )}
          {/* Show command prominently if it's a Bash tool */}
          {command && (
            <div className="mb-2">
              <div className={cn('text-gray-400 mb-1', TEXT_SIZE.label(compact))}>Command:</div>
              <div className="relative">
                <SyntaxHighlighter
                  language="bash"
                  style={oneDark}
                  customStyle={{
                    margin: 0,
                    padding: compact ? '0.25rem' : '0.5rem',
                    fontSize: compact ? '0.65rem' : '0.75rem',
                  }}
                >
                  {command}
                </SyntaxHighlighter>
              </div>
            </div>
          )}
          {/* Show other input fields if not a simple Bash command or todo */}
          {input && !command && !isTodoTool && (
            <div className="mb-2">
              <div className={cn('text-gray-400 mb-1', TEXT_SIZE.label(compact))}>Input:</div>
              <pre className={cn('overflow-x-auto bg-gray-800 p-2 rounded', TEXT_SIZE.label(compact))}>
                {JSON.stringify(input, null, 2)}
              </pre>
            </div>
          )}
          {result && (
            <div>
              <div className={cn('text-gray-400 mb-1', TEXT_SIZE.label(compact))}>Output:</div>
              <SyntaxHighlighter
                language="text"
                style={oneDark}
                customStyle={{
                  margin: 0,
                  padding: compact ? '0.25rem' : '0.5rem',
                  fontSize: compact ? '0.65rem' : '0.75rem',
                  maxHeight: compact ? '200px' : '300px',
                  overflow: 'auto',
                }}
              >
                {result}
              </SyntaxHighlighter>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
