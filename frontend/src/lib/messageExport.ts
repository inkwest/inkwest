import { createElement, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { MessageContent } from '@/types/api';
import { formatFullTimestamp } from '@/lib/utils';

// Split code text into nodes with real <br> between lines. Teams strips inline
// `style` (so white-space:pre-wrap is lost) AND collapses raw "\n", so explicit
// <br> elements are the only line breaks that survive a paste into Teams.
function withLineBreaks(text: string): ReactNode[] {
  const lines = text.replace(/\n$/, '').split('\n');
  return lines.flatMap((line, i) => (i === 0 ? [line] : [createElement('br', { key: i }), line]));
}

const INLINE_CODE_STYLE = {
  fontFamily: 'Consolas,Menlo,monospace',
  background: 'rgba(27,31,35,0.06)',
  padding: '1px 4px',
  borderRadius: '3px',
} as const;

function isInlineCode(className: string | undefined, node: { position?: { start: { line: number }; end: { line: number } } } | undefined): boolean {
  // Inline code = a single source line with no language class.
  return node?.position?.start.line === node?.position?.end.line && !className;
}

// CLEAN renderer — the "real tools" version (Gmail, Docs, Outlook, Slack). Uses a
// proper inline-styled <pre> so a code block is one cohesive, monospace box.
const CLEAN_COMPONENTS: Components = {
  pre: ({ children }) =>
    createElement(
      'pre',
      {
        style: {
          whiteSpace: 'pre-wrap',
          fontFamily: 'Consolas,Menlo,monospace',
          fontSize: '13px',
          background: '#f6f8fa',
          border: '1px solid #e1e4e8',
          borderRadius: '6px',
          padding: '12px',
          margin: '8px 0',
        },
      },
      children,
    ),
  code: ({ className, children, node }) =>
    isInlineCode(className, node)
      ? createElement('code', { style: INLINE_CODE_STYLE }, children)
      : children, // block code: styling lives on the <pre> wrapper
  a: ({ href, children }) => createElement('a', { href, style: { color: '#0969da' } }, children),
};

// TEAMS renderer — Microsoft Teams' paste sanitizer (1) turns <pre> into its own
// widget and relocates it to the end of the message, and (2) strips inline `style`
// off block elements. So this variant emits NO <pre>, gets monospace from the
// <code> ELEMENT (Teams styles it), and line breaks from real <br>. It looks
// chunkier (one code pill per line) but stays in place and stays monospace.
const TEAMS_COMPONENTS: Components = {
  pre: ({ children }) => createElement('div', { style: { margin: '8px 0' } }, children),
  code: ({ className, children, node }) =>
    isInlineCode(className, node)
      ? createElement('code', { style: INLINE_CODE_STYLE }, children)
      : createElement(
          'code',
          {
            style: {
              display: 'block',
              whiteSpace: 'pre-wrap',
              fontFamily: 'Consolas,Menlo,monospace',
              background: '#f6f8fa',
              border: '1px solid #e1e4e8',
              borderRadius: '6px',
              padding: '10px 12px',
            },
          },
          withLineBreaks(String(children)),
        ),
  a: ({ href, children }) => createElement('a', { href, style: { color: '#0969da' } }, children),
};

/**
 * Extract a message's raw Markdown body — the stored source, never the rendered
 * HTML. String content is returned verbatim; block-list content
 * keeps only `text` blocks joined by blank lines (tool_use/tool_result blocks
 * aren't meaningful Markdown). Session-event objects yield an empty string.
 */
export function messageContentToMarkdown(content: MessageContent): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .filter((b) => b.type === 'text' && b.text)
      .map((b) => b.text!.trim())
      .join('\n\n');
  }
  return '';
}

export interface ExportFooterMeta {
  /** Human in the conversation (current user display name). */
  conversationName?: string | null;
  /** Resolved model label for the session (display name, not the raw id). */
  model?: string | null;
  /** Resolved agentic engine label (display name) the turn ran on. */
  engine?: string | null;
  /** Message creation timestamp (ISO) — rendered as the message's own date. */
  date: string;
  sessionId: string;
  messageId: string;
}

// The "Powered by Inkwest …" signature segments, shared by the Markdown and HTML
// flavours. Empty parts are dropped so a missing user/model never leaves a
// dangling separator.
function footerSegments(meta: ExportFooterMeta): string[] {
  return [
    'Powered by Inkwest',
    meta.conversationName ? `Conversation with ${meta.conversationName}` : null,
    meta.engine || null,
    meta.model || null,
    formatFullTimestamp(meta.date),
  ].filter((s): s is string => Boolean(s));
}

function idsLine(meta: ExportFooterMeta): string {
  return `conversation: ${meta.sessionId} · message: ${meta.messageId}`;
}

/**
 * Absolute deep link back to the originating turn. Mirrors the
 * in-app search-hit convention (`/session/<id>?m=<messageId>`, see SessionList):
 * the route + ChatView already resolve `?m=` to scroll/highlight the message
 * client-side within the owner-scoped, already-loaded set — no new endpoint, no
 * IDOR surface. `window.location.origin` makes the pasted footer resolvable from
 * any target; guarded so a non-browser render (tests/SSR) degrades to a relative
 * URL instead of throwing.
 */
function messageDeepLink(meta: ExportFooterMeta): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  // encodeURIComponent leaves ()' untouched; parens in particular would terminate
  // the Markdown `[text](url)` target early, so encode them too. Round-trips
  // cleanly: URLSearchParams/route param decoding restores the original id.
  const enc = (s: string) => encodeURIComponent(s).replace(/\(/g, '%28').replace(/\)/g, '%29');
  return `${origin}/session/${enc(meta.sessionId)}?m=${enc(meta.messageId)}`;
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Assemble the plain-text clipboard payload: the raw Markdown body followed by
 * the attribution signature.
 */
export function buildExportMarkdown(body: string, meta: ExportFooterMeta): string {
  const attribution = `_${footerSegments(meta).join(' · ')}_`;
  // Deep link: the ids become the link text so the IDs stay visible
  // when pasted into a plain editor, while the href carries the resolvable URL.
  const ids = `[${idsLine(meta)}](${messageDeepLink(meta)})`;
  return `${body.trim()}\n\n---\n${attribution}\n\n${ids}\n`;
}

function signatureFooterHtml(meta: ExportFooterMeta): string {
  const signature = escapeHtml(footerSegments(meta).join(' · '));
  const ids = escapeHtml(idsLine(meta));
  // Deep link: wrap the ids in an anchor so a click opens the turn.
  const href = escapeHtml(messageDeepLink(meta));
  return (
    '<hr style="border:none;border-top:1px solid #cccccc;margin:12px 0;">' +
    `<p style="margin:0;color:#666666;font-size:12px;font-style:italic;">${signature}</p>` +
    `<p style="margin:4px 0 0;color:#999999;font-size:11px;font-family:monospace;"><a href="${href}" style="color:#0969da;">${ids}</a></p>`
  );
}

async function renderHtml(body: string, meta: ExportFooterMeta, components: Components): Promise<string> {
  // Loaded on first rich-copy only — keeps react-dom/server off the initial bundle.
  const { renderToStaticMarkup } = await import('react-dom/server');
  const bodyHtml = renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm], components }, body.trim()),
  );
  return `${bodyHtml}${signatureFooterHtml(meta)}`;
}

/**
 * Rich (text/html) payload for capable targets (Gmail, Docs, Outlook, Slack):
 * Markdown rendered to clean inline-styled HTML — code blocks are one cohesive
 * <pre> box — plus the signature footer.
 */
export function buildExportHtml(body: string, meta: ExportFooterMeta): Promise<string> {
  return renderHtml(body, meta, CLEAN_COMPONENTS);
}

/**
 * Teams-specific rich payload: same content but rendered to survive Teams' paste
 * sanitizer (no <pre>, monospace via <code>, line breaks via <br>). Chunkier
 * (a code pill per line) but stays in place and stays monospace.
 */
export function buildExportHtmlTeams(body: string, meta: ExportFooterMeta): Promise<string> {
  return renderHtml(body, meta, TEAMS_COMPONENTS);
}

/**
 * Copy text to the clipboard with a fallback for non-secure contexts and older
 * browsers where `navigator.clipboard` is unavailable. Returns
 * whether the copy succeeded.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Secure-context API present but rejected (e.g. permissions): fall through
      // to the legacy execCommand path below.
    }
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    // Keep it off-screen so the selection doesn't scroll or flash the page.
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/**
 * Copy both a rich (text/html) and a plain (text/plain Markdown) representation in
 * one write, so rich targets (Teams, Slack, Outlook, Docs) keep the formatting
 * while plain targets get the raw Markdown. Degrades to plain-text Markdown when
 * the async Clipboard API / ClipboardItem is unavailable (insecure context / old
 * browser). Returns whether the copy succeeded.
 */
export async function copyRich(html: string, text: string): Promise<boolean> {
  if (typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([
        new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([text], { type: 'text/plain' }),
        }),
      ]);
      return true;
    } catch {
      // Secure-context write rejected — fall back to plain Markdown below.
    }
  }
  return copyToClipboard(text);
}
