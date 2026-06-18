import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { MermaidDiagram } from './MermaidDiagram';
import { MarkdownContent } from './MarkdownContent';

const parse = vi.fn();
const renderDiagram = vi.fn();

// mermaid is dynamically imported by the component; intercept that import.
vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    parse: (...args: unknown[]) => parse(...args),
    render: (...args: unknown[]) => renderDiagram(...args),
  },
}));

describe('MermaidDiagram', () => {
  beforeEach(() => {
    parse.mockReset();
    renderDiagram.mockReset();
  });

  it('renders the mermaid SVG when the source is valid', async () => {
    parse.mockResolvedValue(true);
    renderDiagram.mockResolvedValue({ svg: '<svg id="diagram"></svg>' });

    const { container } = render(<MermaidDiagram code="graph TD; A-->B" />);

    await waitFor(() => expect(container.querySelector('svg')).not.toBeNull());
  });

  it('falls back to the source when the diagram is invalid (e.g. mid-stream)', async () => {
    parse.mockRejectedValue(new Error('incomplete'));

    const { container } = render(<MermaidDiagram code="graph TD; A--" />);

    // parse() rejects, so render() is never reached and the raw source stays visible.
    await waitFor(() => expect(parse).toHaveBeenCalled());
    expect(renderDiagram).not.toHaveBeenCalled();
    expect(container.textContent).toContain('graph TD');
  });

  it('routes a ```mermaid fenced block through MarkdownContent to a diagram', async () => {
    parse.mockResolvedValue(true);
    renderDiagram.mockResolvedValue({ svg: '<svg id="from-markdown"></svg>' });

    const md = ['```mermaid', 'graph TD; A-->B', '```'].join('\n');
    const { container } = render(<MarkdownContent content={md} />);

    await waitFor(() => expect(container.querySelector('svg#from-markdown')).not.toBeNull());
  });
});
