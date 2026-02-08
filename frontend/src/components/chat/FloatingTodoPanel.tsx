import { useState } from 'react';
import { createPortal } from 'react-dom';
import { ListTodo, ChevronDown, ChevronUp, Circle, CheckCircle2, Loader2, Ban } from 'lucide-react';
import { useChatStore, TodoItem } from '@/store/chat';
import { useSettingsStore } from '@/store/settings';
import { cn } from '@/lib/utils';

function TodoItemRow({ todo }: { todo: TodoItem }) {
  const getIcon = () => {
    switch (todo.status) {
      case 'completed':
        return <CheckCircle2 className="h-3 w-3 text-green-400 shrink-0" />;
      case 'in_progress':
        return <Loader2 className="h-3 w-3 text-blue-400 shrink-0 animate-spin" />;
      case 'interrupted':
        return <Ban className="h-3 w-3 text-yellow-500 shrink-0" />;
      default:
        return <Circle className="h-3 w-3 text-gray-500 shrink-0" />;
    }
  };

  return (
    <div className={cn(
      'flex items-start gap-1.5 py-1 px-1.5 rounded text-[11px]',
      todo.status === 'in_progress' && 'bg-blue-900/20',
      todo.status === 'interrupted' && 'bg-yellow-900/20',
      todo.status === 'completed' && 'opacity-60'
    )}>
      {getIcon()}
      <span className={cn(
        'flex-1',
        todo.status === 'completed' && 'line-through text-gray-500',
        todo.status === 'interrupted' && 'text-yellow-500/80'
      )}>
        {todo.status === 'in_progress' && todo.activeForm ? todo.activeForm : todo.content}
      </span>
    </div>
  );
}

export function FloatingTodoPanel() {
  const currentTodos = useChatStore((s) => s.currentTodos);
  const taskListPosition = useSettingsStore((s) => s.taskListPosition);
  const [isExpanded, setIsExpanded] = useState(true);

  if (taskListPosition === 'hidden' || currentTodos.length === 0) {
    return null;
  }

  const completedCount = currentTodos.filter((t) => t.status === 'completed').length;
  const interruptedCount = currentTodos.filter((t) => t.status === 'interrupted').length;
  const inProgressTask = currentTodos.find((t) => t.status === 'in_progress');
  const isInterrupted = interruptedCount > 0 && !inProgressTask;
  const progress = Math.round((completedCount / currentTodos.length) * 100);

  const positionClasses = taskListPosition === 'top'
    ? 'top-20 right-4'
    : 'bottom-28 right-4';

  return createPortal(
    <div className={cn('fixed z-40 w-64 max-w-[calc(100vw-2rem)] text-white', positionClasses)}>
      <div className="bg-gray-800 border border-gray-700 rounded-md shadow-xl overflow-hidden">
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          className="w-full flex items-center gap-1.5 px-2 py-1.5 bg-gray-800 hover:bg-gray-700 transition-colors"
        >
          <ListTodo className={cn('h-3 w-3 shrink-0', isInterrupted ? 'text-yellow-500' : 'text-blue-400')} />
          <span className="text-[11px] font-medium text-gray-200 flex-1 text-left truncate">
            {isInterrupted ? 'Interrupted' : inProgressTask?.activeForm || 'Tasks'}
          </span>
          <span className={cn(
            'text-[10px] px-1 py-0.5 rounded shrink-0',
            completedCount === currentTodos.length
              ? 'bg-green-900/50 text-green-400'
              : isInterrupted
                ? 'bg-yellow-900/50 text-yellow-500'
                : 'bg-blue-900/50 text-blue-400'
          )}>
            {completedCount}/{currentTodos.length}
          </span>
          {isExpanded ? (
            <ChevronDown className="h-3 w-3 text-gray-400 shrink-0" />
          ) : (
            <ChevronUp className="h-3 w-3 text-gray-400 shrink-0" />
          )}
        </button>

        <div className="h-0.5 bg-gray-700">
          <div
            className={cn(
              'h-full transition-all duration-300',
              completedCount === currentTodos.length ? 'bg-green-500' : isInterrupted ? 'bg-yellow-500' : 'bg-blue-500'
            )}
            style={{ width: `${progress}%` }}
          />
        </div>

        {isExpanded && (
          <div className="max-h-48 overflow-y-auto p-1.5 space-y-0.5">
            {currentTodos.map((todo, index) => (
              <TodoItemRow key={index} todo={todo} />
            ))}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
