import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TokenGlyphs, TOKEN_GLYPH_TITLES } from './TokenGlyphs';

describe('TokenGlyphs', () => {
  it('always renders input/output, cache segments only when non-zero', () => {
    const { container } = render(<TokenGlyphs input={12_000} output={3_000} />);
    expect(container.textContent).toContain('12.0k↓');
    expect(container.textContent).toContain('3.0k↑');
    expect(container.textContent).not.toContain('⚡');
    expect(container.textContent).not.toContain('⊕');
  });

  it('renders cache segments with their explanatory titles', () => {
    render(<TokenGlyphs input={10} output={5} cacheRead={8_000} cacheWrite={2_000} />);
    expect(screen.getByTitle(TOKEN_GLYPH_TITLES.cacheRead(8_000)).textContent).toContain('8.0k');
    expect(screen.getByTitle(TOKEN_GLYPH_TITLES.cacheWrite(2_000)).textContent).toContain('2.0k');
  });

  it('cache-read glyph carries U+FE0E so it renders monochrome, not emoji', () => {
    // Regression guard for 4a36af4: without the variation selector the default emoji
    // presentation of U+26A1 ignores the span's CSS color.
    const { container } = render(<TokenGlyphs input={1} output={1} cacheRead={100} />);
    expect(container.textContent).toContain('⚡︎');
  });
});
