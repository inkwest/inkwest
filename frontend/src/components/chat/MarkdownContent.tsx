import { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { visit } from 'unist-util-visit';
import { CodeBlock } from './CodeBlock';
import { MermaidDiagram } from './MermaidDiagram';
import { useSettingsStore } from '@/store/settings';
import { cn, TEXT_SIZE } from '@/lib/utils';
import type { Components } from 'react-markdown';

interface Props {
  content: string;
  // Treat single newlines as hard line breaks (CommonMark collapses them to a
  // space). Used for user messages so the multi-line text the user typed is
  // echoed verbatim instead of reflowed onto one line.
  breaks?: boolean;
}

// Mini remark-breaks: split each text node on "\n" into text + hardbreak nodes
// so soft line endings render as <br>. Equivalent to the remark-breaks plugin,
// inlined because the org's package policy blocks adding it as a dependency.
// A leading/trailing newline yields an empty text node here; acceptable because
// the only caller (user messages) trims input before send, so it can't occur.
function remarkHardBreaks() {
  return (tree: unknown) => {
    visit(tree as never, 'text', (node: { value?: string }, index, parent) => {
      if (!parent || typeof index !== 'number' || !node.value?.includes('\n')) return;
      const parts = node.value.split('\n');
      const replacement = parts.flatMap((part, i) =>
        i === 0 ? [{ type: 'text', value: part }] : [{ type: 'break' }, { type: 'text', value: part }]
      );
      (parent as { children: unknown[] }).children.splice(index, 1, ...replacement);
      return index + replacement.length;
    });
  };
}

// Hoisted so identities are stable across renders (react-markdown reprocesses
// when the plugin array changes).
const remarkPlugins = [remarkGfm];
const remarkPluginsWithBreaks = [remarkGfm, remarkHardBreaks];

export function MarkdownContent({ content, breaks = false }: Props) {
  const compact = useSettingsStore((s) => s.compactToolCards);

  // Memoize so the custom-renderer function identities stay stable across
  // re-renders. react-markdown uses these as element *types*; a fresh object
  // every render would remount the whole subtree — resetting MermaidDiagram's
  // rendered-SVG state and flashing the raw source (e.g. on copy / menu clicks).
  const components: Components = useMemo(() => ({
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
  }), [compact]);

  return (
    <div className={cn(
      '[&>p]:mb-2 [&>p:last-child]:mb-0 [&>ul]:mb-2 [&>ol]:mb-2 [&>pre]:mb-2 [&>ul]:list-disc [&>ul]:pl-4 [&>ol]:list-decimal [&>ol]:pl-4',
      // GFM task lists carry their own checkboxes; drop the inherited disc/indent
      '[&_.contains-task-list]:list-none [&_.contains-task-list]:pl-0',
      TEXT_SIZE.body(compact), compact && 'leading-relaxed'
    )}>
      <ReactMarkdown remarkPlugins={breaks ? remarkPluginsWithBreaks : remarkPlugins} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
