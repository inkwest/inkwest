import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './CodeBlock';
import { MermaidDiagram } from './MermaidDiagram';
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

      // Render fenced ```mermaid blocks as graphical diagrams (falls back to
      // source while streaming / on invalid syntax).
      if (!isInline && language === 'mermaid') {
        return <MermaidDiagram code={codeString} compact={compact} />;
      }

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
    // GFM tables: wrap for horizontal overflow + dark-theme borders
    table({ children }) {
      return (
        <div className="my-2 overflow-x-auto">
          <table className="w-full border-collapse text-left">{children}</table>
        </div>
      );
    },
    // Forward `style` so GFM column alignment (:---:, ---:) is honored
    th({ children, style }) {
      return (
        <th style={style} className="border border-gray-700 bg-gray-800 px-2 py-1 font-semibold">
          {children}
        </th>
      );
    },
    td({ children, style }) {
      return (
        <td style={style} className="border border-gray-700 px-2 py-1 align-top">
          {children}
        </td>
      );
    },
  };

  return (
    <div className={cn(
      '[&>p]:mb-2 [&>p:last-child]:mb-0 [&>ul]:mb-2 [&>ol]:mb-2 [&>pre]:mb-2 [&>ul]:list-disc [&>ul]:pl-4 [&>ol]:list-decimal [&>ol]:pl-4',
      // GFM task lists carry their own checkboxes; drop the inherited disc/indent
      '[&_.contains-task-list]:list-none [&_.contains-task-list]:pl-0',
      TEXT_SIZE.body(compact), compact && 'leading-relaxed'
    )}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
