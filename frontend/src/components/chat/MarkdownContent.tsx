import ReactMarkdown from 'react-markdown';
import { CodeBlock } from './CodeBlock';
import { useSettingsStore } from '@/store/settings';
import { cn, TEXT_SIZE } from '@/lib/utils';
import type { Components } from 'react-markdown';

interface Props {
  content: string;
}

export function MarkdownContent({ content }: Props) {
  const compact = useSettingsStore((s) => s.compactToolCards);

  const components: Components = {
    code({ className, children, node }) {
      const match = /language-(\w+)/.exec(className || '');
      const language = match ? match[1] : undefined;
      const codeString = String(children).replace(/\n$/, '');
      // Check if inline by seeing if parent is pre (block) or not
      const isInline = node?.position?.start.line === node?.position?.end.line && !className;

      return (
        <CodeBlock
          language={language}
          inline={isInline}
          compact={compact}
        >
          {codeString}
        </CodeBlock>
      );
    },
    // Style links
    a({ children, href }) {
      return (
        <a
          href={href}
          className="text-blue-400 hover:underline"
          target="_blank"
          rel="noopener noreferrer"
        >
          {children}
        </a>
      );
    },
    // Remove pre wrapper since CodeBlock handles it
    pre({ children }) {
      return <>{children}</>;
    },
  };

  return (
    <div className={cn(
      '[&>p]:mb-2 [&>p:last-child]:mb-0 [&>ul]:mb-2 [&>ol]:mb-2 [&>pre]:mb-2 [&>ul]:list-disc [&>ul]:pl-4 [&>ol]:list-decimal [&>ol]:pl-4',
      TEXT_SIZE.body(compact), compact && 'leading-relaxed'
    )}>
      <ReactMarkdown components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
