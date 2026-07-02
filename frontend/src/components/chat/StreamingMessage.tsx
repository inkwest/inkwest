import { useChatStore } from '@/store/chat';
import { useSettingsStore } from '@/store/settings';
import { ToolCallCard } from './ToolCallCard';
import { MarkdownContent } from './MarkdownContent';
import { cn, TEXT_SIZE } from '@/lib/utils';

export function StreamingMessage() {
  // Narrow selector: streaming pushes many unrelated store updates (cost, metrics) per
  // turn — subscribe only to streamingBlocks so those don't re-render (and re-highlight)
  // the whole streamed message.
  const streamingBlocks = useChatStore((s) => s.streamingBlocks);
  const compact = useSettingsStore((s) => s.compactToolCards);

  // Render streaming blocks inline in order
  const renderBlocks = () => {
    if (streamingBlocks.length === 0) {
      return (
        <div className="flex items-center gap-2">
          <div className={cn(compact ? 'w-1.5 h-1.5' : 'w-2 h-2', 'bg-blue-500 rounded-full animate-pulse')} />
          <span className={cn('text-gray-400', TEXT_SIZE.label(compact))}>Thinking...</span>
        </div>
      );
    }

    return streamingBlocks.map((block, index) => {
      if (block.type === 'text') {
        return <MarkdownContent key={index} content={block.content} />;
      }
      if (block.type === 'tool_use') {
        return (
          <ToolCallCard
            key={index}
            name={block.name}
            input={block.input}
            isLoading={block.isLoading}
          />
        );
      }
      if (block.type === 'tool_result') {
        return (
          <ToolCallCard
            key={index}
            name={block.name}
            result={block.content}
            isError={block.is_error}
          />
        );
      }
      return null;
    });
  };

  return (
    <div className="flex justify-start">
      <div className={cn(
        'max-w-[80%] rounded-lg bg-gray-700 text-gray-100 overflow-hidden min-w-0',
        compact ? 'px-3 py-1.5' : 'px-4 py-2'
      )}>
        {renderBlocks()}
      </div>
    </div>
  );
}
