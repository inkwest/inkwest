import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { captureLinkPrefill, takeLinkPrefill } from './linkPrefill';

const go = (url: string) => window.history.replaceState(null, '', url);
const link = (text: string) => `/#prompt=${encodeURIComponent(text)}`;
const TEN_MIN = 10 * 60 * 1000;

describe('linkPrefill', () => {
  beforeEach(() => {
    sessionStorage.clear();
    go('/');
  });
  afterEach(() => {
    vi.restoreAllMocks();
    go('/');
  });

  it('005-AC1: stores the prompt and strips the fragment', () => {
    go(link('list pods'));
    captureLinkPrefill();
    expect(window.location.hash).toBe('');
    expect(window.location.href.endsWith('/')).toBe(true);
    expect(takeLinkPrefill()).toBe('list pods');
  });

  it('005-AC1: round-trips a tricky string exactly', () => {
    const text = 'line1\nline2\r\n& # + % %20 =?\t🚀 <b>tag</b> é';
    go(link(text));
    captureLinkPrefill();
    expect(takeLinkPrefill()).toBe(text);
  });

  it('005-AC1: decodes form-style, a raw + is a space and %2B a plus', () => {
    go('/#prompt=a+b');
    captureLinkPrefill();
    expect(takeLinkPrefill()).toBe('a b');
    go('/#prompt=a%2Bb');
    captureLinkPrefill();
    expect(takeLinkPrefill()).toBe('a+b');
  });

  it('keeps the other fragment keys and the query string', () => {
    go(`/?x=1#a=1&prompt=${encodeURIComponent('hi')}&b=2`);
    captureLinkPrefill();
    expect(window.location.search).toBe('?x=1');
    expect(window.location.hash).toBe('#a=1&b=2');
    expect(takeLinkPrefill()).toBe('hi');
  });

  it('ignores a prompt fragment on a non-root path', () => {
    go('/session/abc#prompt=hi');
    captureLinkPrefill();
    expect(window.location.hash).toBe('#prompt=hi');
    expect(takeLinkPrefill()).toBeNull();
  });

  it('leaves a fragment without prompt untouched', () => {
    go('/#cluster=prod');
    captureLinkPrefill();
    expect(window.location.hash).toBe('#cluster=prod');
    expect(takeLinkPrefill()).toBeNull();
  });

  it('does not store an empty prompt but still strips the key', () => {
    go('/#prompt=&a=1');
    captureLinkPrefill();
    expect(window.location.hash).toBe('#a=1');
    expect(takeLinkPrefill()).toBeNull();
  });

  it('005-AC4: take is one-shot', () => {
    go(link('once'));
    captureLinkPrefill();
    expect(takeLinkPrefill()).toBe('once');
    expect(takeLinkPrefill()).toBeNull();
  });

  it('005-AC5: survives the login round-trip (reboots without the fragment)', () => {
    go(link('after login'));
    captureLinkPrefill();
    // Each full-page load boots capture again: OIDC callback, then a bare `/` with no fragment.
    go('/auth/callback?code=abc&state=xyz');
    captureLinkPrefill();
    go('/');
    captureLinkPrefill();
    expect(takeLinkPrefill()).toBe('after login');
  });

  it('returns a prefill taken within the max age', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
    go(link('fresh'));
    captureLinkPrefill();
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000 + TEN_MIN);
    expect(takeLinkPrefill()).toBe('fresh');
  });

  it('drops a stale prefill and removes the entry', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
    go(link('stale'));
    captureLinkPrefill();
    vi.spyOn(Date, 'now').mockReturnValue(1_000_000 + TEN_MIN + 1);
    expect(takeLinkPrefill()).toBeNull();
    expect(sessionStorage.length).toBe(0);
  });

  it.each([
    'not json',
    'null',
    '{"prompt":"x"}',
    '{"prompt":42,"at":1000000}',
    '{"prompt":"x","at":"1000000"}',
  ])(
    'drops a malformed entry: %s',
    (raw) => {
      vi.spyOn(Date, 'now').mockReturnValue(1_000_000);
      sessionStorage.setItem('inkwest-link-prefill', raw);
      expect(takeLinkPrefill()).toBeNull();
      expect(sessionStorage.length).toBe(0);
    },
  );

  it('survives blocked storage: no throw, take returns null', () => {
    vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    vi.spyOn(sessionStorage, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    go(link('lost'));
    expect(() => captureLinkPrefill()).not.toThrow();
    expect(window.location.hash).toBe('');
    expect(takeLinkPrefill()).toBeNull();
  });

  it('005-AC10: a 150 000-char ASCII prompt round-trips exactly', () => {
    const ascii = Array.from({ length: 95 }, (_, i) => String.fromCharCode(32 + i)).join('') + '\n';
    const text = ascii.repeat(Math.ceil(150_000 / ascii.length)).slice(0, 150_000);
    go(link(text));
    captureLinkPrefill();
    expect(takeLinkPrefill()).toBe(text);
  });

  it('005-AC10: a 20 000-char accents + emoji prompt round-trips exactly', () => {
    const chars = Array.from('éàçüñøß ÉÈ🚀🔥👩‍💻 ');
    const text = Array.from({ length: 20_000 }, (_, i) => chars[i % chars.length]).join('');
    go(link(text));
    captureLinkPrefill();
    expect(takeLinkPrefill()).toBe(text);
  });
});
