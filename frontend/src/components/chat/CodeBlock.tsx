import { useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { cn, TEXT_SIZE } from '@/lib/utils';

interface Props {
  children: string;
  language?: string;
  inline?: boolean;
  compact?: boolean;
}

export function CodeBlock({ children, language, inline, compact }: Props) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(children);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Inline code - simple styled span
  if (inline) {
    return (
      <code className={cn(
        'px-1 py-0.5 bg-gray-800 rounded font-mono text-pink-400',
        TEXT_SIZE.code(compact ?? false)
      )}>
        {children}
      </code>
    );
  }

  // Block code - with syntax highlighting and copy button
  return (
    <div className={cn('relative group', compact ? 'my-1' : 'my-2')}>
      <div className={cn(
        'absolute opacity-0 group-hover:opacity-100 transition-opacity',
        compact ? 'right-1 top-1' : 'right-2 top-2'
      )}>
        <button
          onClick={handleCopy}
          className={cn(
            'bg-gray-700 hover:bg-gray-600 rounded text-gray-300',
            compact ? 'p-1' : 'p-1.5'
          )}
          title="Copy code"
        >
          {copied ? (
            <Check className={cn(compact ? 'h-3 w-3' : 'h-4 w-4', 'text-green-400')} />
          ) : (
            <Copy className={cn(compact ? 'h-3 w-3' : 'h-4 w-4')} />
          )}
        </button>
      </div>
      <div className={cn('border border-gray-600 overflow-hidden', compact ? 'rounded' : 'rounded-lg')}>
        <SyntaxHighlighter
          language={language || 'text'}
          style={oneDark}
          customStyle={{
            margin: 0,
            padding: compact ? '0.5rem' : '1rem',
            fontSize: compact ? '0.75rem' : '0.875rem',
            background: '#1e1e1e',
          }}
        >
          {children}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}
