import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';

// Stub the Mermaid child: it pulls in the heavy `mermaid` package, which is
// irrelevant to these tests and may not be installed in CI. Mirrors how
// MessageBubble.test mocks its heavy children.
vi.mock('./MermaidDiagram', () => ({
  MermaidDiagram: ({ code }: { code: string }) => <pre data-testid="mermaid">{code}</pre>,
}));

import { MarkdownContent } from './MarkdownContent';

// Guards the GFM wiring (remark-gfm + table/th/td renderers). Without the
// remarkPlugins prop, react-markdown parses CommonMark only and pipe tables
// fall through as raw text — the bug this component was fixed for.
describe('MarkdownContent GFM tables', () => {
  it('renders a pipe table as a real <table> with header and body cells', () => {
    const md = ['| Group | Examples |', '|---|---|', '| acme | team/project |'].join('\n');
    const { container } = render(<MarkdownContent content={md} />);

    expect(container.querySelector('table')).not.toBeNull();
    expect(container.querySelectorAll('th')).toHaveLength(2);
    expect(container.querySelectorAll('td')).toHaveLength(2);
    // The separator row must be consumed by the parser, not shown as text.
    expect(container.textContent).not.toContain('|---|');
  });

  it('honors GFM column alignment markers', () => {
    const md = ['| L | C | R |', '|:--|:-:|--:|', '| a | b | c |'].join('\n');
    const { container } = render(<MarkdownContent content={md} />);

    const align = Array.from(container.querySelectorAll('td')).map((c) => c.style.textAlign);
    expect(align).toEqual(['left', 'center', 'right']);
  });

  it('renders GFM strikethrough', () => {
    const { container } = render(<MarkdownContent content="~~gone~~" />);
    expect(container.querySelector('del')).not.toBeNull();
  });
});

// Guards the `breaks` opt-in used for user messages. CommonMark collapses a
// single newline into a space; with `breaks` it must render a <br> instead so
// the multi-line text the user typed is echoed verbatim.
describe('MarkdownContent soft line breaks', () => {
  it('renders a <br> for single newlines when breaks is set', () => {
    const { container } = render(<MarkdownContent content={'first\nsecond\nthird'} breaks />);
    expect(container.querySelectorAll('br')).toHaveLength(2);
  });

  it('collapses single newlines (no <br>) by default', () => {
    const { container } = render(<MarkdownContent content={'first\nsecond'} />);
    expect(container.querySelector('br')).toBeNull();
  });

  it('still renders markdown when breaks is set', () => {
    // breaks must compose with GFM, not replace it.
    const { container } = render(<MarkdownContent content={'**bold**\nplain'} breaks />);
    expect(container.querySelector('strong')).not.toBeNull();
    expect(container.querySelector('br')).not.toBeNull();
  });
});
