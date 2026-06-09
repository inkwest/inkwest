import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
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
