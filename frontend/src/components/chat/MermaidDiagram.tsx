import { useEffect, useId, useState } from 'react';
import { CodeBlock } from './CodeBlock';
import { cn } from '@/lib/utils';

interface Props {
  code: string;
  compact?: boolean;
}

// Lazy-load mermaid (~600KB) so it only enters the bundle when a diagram is
// actually rendered, and initialize it exactly once across the app.
let initPromise: Promise<typeof import('mermaid').default> | null = null;

function loadMermaid() {
  if (!initPromise) {
    initPromise = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'dark',
        // Diagrams come from the model — sanitize labels and disable scripts/click handlers.
        securityLevel: 'strict',
        fontFamily: 'inherit',
      });
      return mermaid;
    });
  }
  return initPromise;
}

export function MermaidDiagram({ code, compact }: Props) {
  const [svg, setSvg] = useState<string | null>(null);
  // useId gives a stable, collision-free DOM id for mermaid's render target.
  const renderId = `mermaid-${useId().replace(/[^a-zA-Z0-9]/g, '')}`;

  useEffect(() => {
    let cancelled = false;
    const source = code.trim();
    if (!source) {
      setSvg(null);
      return;
    }
    (async () => {
      try {
        const mermaid = await loadMermaid();
        // parse() throws on incomplete/invalid input — expected mid-stream while
        // the fenced block is still arriving; we fall back to the source until valid.
        await mermaid.parse(source);
        const { svg } = await mermaid.render(renderId, source);
        if (!cancelled) setSvg(svg);
      } catch {
        if (!cancelled) setSvg(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, renderId]);

  // No valid render yet (streaming, invalid syntax, or load failure): show source.
  if (!svg) {
    return (
      <CodeBlock language="mermaid" compact={compact}>
        {code}
      </CodeBlock>
    );
  }

  return (
    <div
      className={cn(
        'flex justify-center overflow-x-auto rounded-lg border border-gray-600 bg-gray-900/50',
        '[&_svg]:h-auto [&_svg]:max-w-full',
        compact ? 'my-1 p-2' : 'my-2 p-3'
      )}
      // Safe: mermaid sanitizes output under securityLevel 'strict'.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
