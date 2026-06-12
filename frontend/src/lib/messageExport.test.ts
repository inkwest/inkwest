import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  messageContentToMarkdown,
  buildExportMarkdown,
  buildExportHtml,
  buildExportHtmlTeams,
  copyToClipboard,
  copyRich,
} from './messageExport';
import { formatFullTimestamp } from './utils';

describe('messageContentToMarkdown', () => {
  it('returns string content verbatim', () => {
    expect(messageContentToMarkdown('# Title\n\nbody')).toBe('# Title\n\nbody');
  });

  it('joins text blocks and drops tool blocks', () => {
    const md = messageContentToMarkdown([
      { type: 'text', text: 'First' },
      { type: 'tool_use', name: 'kubectl_get', input: {} },
      { type: 'text', text: 'Second' },
      { type: 'tool_result', content: 'output' },
    ]);
    expect(md).toBe('First\n\nSecond');
  });

  it('yields an empty string for a session-event object', () => {
    expect(messageContentToMarkdown({ type: 'session_event', subject: 'aws', action: 'granted' })).toBe('');
  });
});

describe('buildExportMarkdown', () => {
  const meta = {
    conversationName: 'Damien Degois',
    model: 'Claude Opus 4.8',
    date: '2026-06-11T14:30:00Z',
    sessionId: 'sess-1',
    messageId: 'msg-1',
  };

  it('appends an attribution footer and id line to the body', () => {
    const out = buildExportMarkdown('Here are your pods.', meta);
    expect(out).toContain('Here are your pods.');
    expect(out).toContain('Powered by Inkwest');
    expect(out).toContain('Conversation with Damien Degois');
    expect(out).toContain('Claude Opus 4.8');
    expect(out).toContain(formatFullTimestamp(meta.date));
    expect(out).toContain('`conversation: sess-1 · message: msg-1`');
    expect(out.endsWith('\n')).toBe(true);
  });

  it('drops the name and model segments when absent (no dangling separators)', () => {
    const out = buildExportMarkdown('body', { ...meta, conversationName: null, model: null });
    expect(out).not.toContain('Conversation with');
    expect(out).not.toContain('··');
    expect(out).toContain(`_Powered by Inkwest · ${formatFullTimestamp(meta.date)}_`);
  });

  it('trims the body before appending the footer', () => {
    const out = buildExportMarkdown('  spaced  \n', meta);
    expect(out.startsWith('spaced\n\n---')).toBe(true);
  });
});

describe('buildExportHtml', () => {
  const meta = {
    conversationName: 'Damien Degois',
    model: 'Claude Opus 4.8',
    date: '2026-06-11T14:30:00Z',
    sessionId: 'sess-1',
    messageId: 'msg-1',
  };

  it('renders Markdown to semantic HTML and appends the signature footer', async () => {
    const html = await buildExportHtml('# Title\n\n**bold** and a list:\n\n- one\n- two', meta);
    expect(html).toContain('<h1>Title</h1>');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<li>one</li>');
    // Signature line must be present (with inline styles so it survives paste).
    expect(html).toContain('Powered by Inkwest');
    expect(html).toContain('Conversation with Damien Degois');
    expect(html).toContain('Claude Opus 4.8');
    expect(html).toContain('conversation: sess-1 · message: msg-1');
    expect(html).toContain('style=');
  });

  it('renders fenced code as a cohesive <pre> block (clean, for capable targets)', async () => {
    const html = await buildExportHtml('```js\nconst x = 1;\nconst y = 2;\n```', meta);
    expect(html).toContain('<pre');
    expect(html).toContain('const x = 1;');
    expect(html).toContain('const y = 2;');
  });

  it('escapes HTML-significant characters in the signature (display name)', async () => {
    const html = await buildExportHtml('body', { ...meta, conversationName: '<script>x</script>' });
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>x');
  });
});

describe('buildExportHtmlTeams', () => {
  const meta = {
    conversationName: 'Damien Degois',
    model: 'Claude Opus 4.8',
    date: '2026-06-11T14:30:00Z',
    sessionId: 'sess-1',
    messageId: 'msg-1',
  };

  it('renders fenced code Teams-safe: no <pre>, monospace <code>, real <br> breaks', async () => {
    const html = await buildExportHtmlTeams('```js\nconst x = 1;\nconst y = 2;\n```', meta);
    // No <pre> (Teams relocates it); monospace via <code> (survives style-stripping);
    // line breaks via real <br> (raw "\n" is collapsed by Teams).
    expect(html).not.toContain('<pre');
    expect(html).toContain('<code');
    expect(html).toContain('<br');
    expect(html).toContain('const x = 1;');
    expect(html).toContain('const y = 2;');
    expect(html).toContain('Powered by Inkwest'); // signature still present
  });
});

describe('copyRich', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('writes both text/html and text/plain via ClipboardItem when available', async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    const seen: Record<string, unknown>[] = [];
    class FakeClipboardItem {
      constructor(items: Record<string, unknown>) {
        seen.push(items);
      }
    }
    vi.stubGlobal('ClipboardItem', FakeClipboardItem);
    vi.stubGlobal('navigator', { clipboard: { write } });

    expect(await copyRich('<p>hi</p>', 'hi')).toBe(true);
    expect(write).toHaveBeenCalledTimes(1);
    expect(Object.keys(seen[0])).toEqual(['text/html', 'text/plain']);
  });

  it('falls back to plain-text writeText when ClipboardItem is unavailable', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('ClipboardItem', undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });

    expect(await copyRich('<p>hi</p>', 'hi-markdown')).toBe(true);
    expect(writeText).toHaveBeenCalledWith('hi-markdown');
  });
});

describe('copyToClipboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('uses navigator.clipboard when available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    expect(await copyToClipboard('hello')).toBe(true);
    expect(writeText).toHaveBeenCalledWith('hello');
  });

  it('falls back to execCommand when clipboard API is missing', async () => {
    vi.stubGlobal('navigator', {});
    const exec = vi.fn().mockReturnValue(true);
    // jsdom has no execCommand; provide one for the fallback path.
    (document as unknown as { execCommand: typeof exec }).execCommand = exec;
    expect(await copyToClipboard('hello')).toBe(true);
    expect(exec).toHaveBeenCalledWith('copy');
  });
});

beforeEach(() => {
  // Keep tests independent of any global clipboard stub leaking across files.
  vi.restoreAllMocks();
});
