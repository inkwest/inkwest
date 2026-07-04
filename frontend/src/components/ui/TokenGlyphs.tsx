import { formatTokenCount } from '@/lib/utils';

// Per-symbol hover titles for the token glyph line (bubble footer, header/session badges).
// Cache is INPUT-side only (Bedrock/Anthropic never cache output): the glyphs map to
// cache-read (served from the prompt cache, ~0.1x input rate) and cache-write (~1.25x,
// 2x for 1h-TTL writes).
export const TOKEN_GLYPH_TITLES = {
  input: 'Fresh input tokens (billed at the model input rate)',
  output: 'Output tokens',
  cacheRead: (n: number) =>
    `Cache read: ${n.toLocaleString()} tokens (served from prompt cache, billed ~0.1x input rate)`,
  cacheWrite: (n: number) =>
    `Cache write: ${n.toLocaleString()} tokens (written to prompt cache, billed ~1.25x input rate; 2x for 1h-TTL writes)`,
} as const;

/**
 * Compact token glyph line: `12k↓ 3k↑ 8k⚡ 2k⊕` — fresh input, output, cache-read,
 * cache-write, in that fixed order. The cache glyphs render only when non-zero so a
 * no-cache turn stays quiet; each segment carries its OWN title (the glyphs are cryptic).
 * Shared by the chat bubble footer, the header badge and the session-list badge.
 */
export function TokenGlyphs({
  input,
  output,
  cacheRead = 0,
  cacheWrite = 0,
  className,
}: {
  input: number;
  output: number;
  cacheRead?: number;
  cacheWrite?: number;
  className?: string;
}) {
  return (
    <span className={className}>
      <span title={TOKEN_GLYPH_TITLES.input}>{formatTokenCount(input)}&darr;</span>{' '}
      <span title={TOKEN_GLYPH_TITLES.output}>{formatTokenCount(output)}&uarr;</span>
      {cacheRead > 0 && (
        <>
          {' '}
          {/* U+FE0E forces the text (monochrome) presentation — the default emoji
              rendering of U+26A1 ignores the span's CSS color. */}
          <span title={TOKEN_GLYPH_TITLES.cacheRead(cacheRead)}>
            {formatTokenCount(cacheRead)}&#x26a1;&#xfe0e;
          </span>
        </>
      )}
      {cacheWrite > 0 && (
        <>
          {' '}
          <span title={TOKEN_GLYPH_TITLES.cacheWrite(cacheWrite)}>
            {formatTokenCount(cacheWrite)}&oplus;
          </span>
        </>
      )}
    </span>
  );
}
