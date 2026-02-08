import { useState } from 'react';
import { ChevronDown, ChevronRight, Brain } from 'lucide-react';

interface Props {
  content: string;
  defaultExpanded?: boolean;
}

export function ThinkingBlock({ content, defaultExpanded = false }: Props) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  // Truncate preview to first 100 chars
  const preview = content.length > 100
    ? content.substring(0, 100) + '...'
    : content;

  return (
    <div className="my-2 border border-purple-500/30 rounded-lg overflow-hidden bg-purple-950/20">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-purple-900/20 text-left"
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 text-purple-400" />
        ) : (
          <ChevronRight className="h-4 w-4 text-purple-400" />
        )}
        <Brain className="h-4 w-4 text-purple-400" />
        <span className="text-sm text-purple-300 font-medium">AI Reasoning</span>
        {!expanded && (
          <span className="ml-2 text-xs text-gray-500 truncate flex-1">
            {preview}
          </span>
        )}
      </button>

      {expanded && (
        <div className="px-4 py-3 border-t border-purple-500/20">
          <pre className="text-sm text-gray-300 whitespace-pre-wrap font-mono leading-relaxed">
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}
