import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, fireEvent } from '@testing-library/react';
import { AnnouncementBanners } from './AnnouncementBanners';
import { useAnnouncementsStore } from '@/store/announcements';
import type { Announcement } from '@/types/api';

const KEY = 'inkwest.announcements.dismissed.v1';
const MAX_TIMEOUT_MS = 2 ** 31 - 1;

const banner = (over: Partial<Announcement> = {}): Announcement => ({
  id: 'a1',
  message: 'Maintenance tonight',
  level: 'info',
  color: null,
  text_color: null,
  opacity: 100,
  dismissible: true,
  ends_at: null,
  updated_at: '2026-10-28T10:00:00Z',
  ...over,
});

const refresh = vi.fn(async () => {});
const show = (...items: Announcement[]) => useAnnouncementsStore.setState({ items, loaded: true, refresh });
const status = () => screen.getByRole('status');

describe('AnnouncementBanners', () => {
  beforeEach(() => {
    refresh.mockClear();
    localStorage.clear();
    useAnnouncementsStore.setState({ items: [], loaded: false, refresh });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('renders nothing when there is nothing to show', () => {
    const { container } = render(<AnnouncementBanners />);
    expect(container.firstChild).toBeNull();
  });

  it('004-AC1: shows each served banner, oldest first, as a status', () => {
    show(banner({ id: 'a', message: 'first' }), banner({ id: 'b', message: 'second' }));
    render(<AnnouncementBanners />);
    expect(screen.getAllByRole('status').map((el) => el.textContent)).toEqual(['first', 'second']);
  });

  describe('004-AC3: refresh triggers', () => {
    it('refreshes once on mount', () => {
      render(<AnnouncementBanners />);
      expect(refresh).toHaveBeenCalledTimes(1);
    });

    it('refreshes when the tab becomes visible, not when it is hidden', () => {
      render(<AnnouncementBanners />);
      const setVisibility = (v: DocumentVisibilityState) => {
        vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(v);
        document.dispatchEvent(new Event('visibilitychange'));
      };
      setVisibility('hidden');
      expect(refresh).toHaveBeenCalledTimes(1);
      setVisibility('visible');
      expect(refresh).toHaveBeenCalledTimes(2);
    });

    it('stops listening on unmount', () => {
      const { unmount } = render(<AnnouncementBanners />);
      unmount();
      document.dispatchEvent(new Event('visibilitychange'));
      expect(refresh).toHaveBeenCalledTimes(1);
    });
  });

  describe('004-AC4: dismissal', () => {
    it('stays hidden after a reload, shows again once the admin edits it', () => {
      show(banner());
      const first = render(<AnnouncementBanners />);
      fireEvent.click(screen.getByRole('button', { name: 'Dismiss announcement' }));
      expect(screen.queryByRole('status')).toBeNull();
      expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(['a1:2026-10-28T10:00:00Z']);
      first.unmount();

      render(<AnnouncementBanners />); // reload: state re-read from storage
      expect(screen.queryByRole('status')).toBeNull();

      act(() => show(banner({ updated_at: '2026-10-28T11:00:00Z' })));
      expect(status()).toHaveTextContent('Maintenance tonight');
    });

    it('hides only the closed banner', () => {
      show(banner({ id: 'a', message: 'one' }), banner({ id: 'b', message: 'two' }));
      render(<AnnouncementBanners />);
      fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss announcement' })[0]);
      expect(status()).toHaveTextContent('two');
    });

    it('does not prune stored dismissals before a fetch has answered', () => {
      localStorage.setItem(KEY, JSON.stringify(['gone:t', 'a1:2026-10-28T10:00:00Z']));
      useAnnouncementsStore.setState({ items: [], loaded: false, refresh });
      render(<AnnouncementBanners />);
      expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(['gone:t', 'a1:2026-10-28T10:00:00Z']);

      act(() => show(banner()));
      expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(['a1:2026-10-28T10:00:00Z']);
      expect(screen.queryByRole('status')).toBeNull();
    });

    it.each([
      ['corrupt JSON', '{not json'],
      ['a non-array', '{"a1:2026-10-28T10:00:00Z":true}'],
      ['non-string entries', '[1, null, {}]'],
    ])('renders despite %s in storage', (_label, raw) => {
      localStorage.setItem(KEY, raw);
      show(banner());
      render(<AnnouncementBanners />);
      expect(status()).toHaveTextContent('Maintenance tonight');
    });

    it('still hides for this page when storage refuses the write', () => {
      vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
        throw new DOMException('quota', 'QuotaExceededError');
      });
      show(banner());
      render(<AnnouncementBanners />);
      fireEvent.click(screen.getByRole('button', { name: 'Dismiss announcement' }));
      expect(screen.queryByRole('status')).toBeNull();
    });

    it('004-AC4: a close keeps a dismissal another tab stored after mount', () => {
      show(banner({ id: 'x', message: 'x' }), banner({ id: 'y', message: 'y' }));
      render(<AnnouncementBanners />);
      localStorage.setItem(KEY, JSON.stringify(['x:2026-10-28T10:00:00Z'])); // tab A closes x
      fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss announcement' })[1]); // tab B closes y
      expect(new Set(JSON.parse(localStorage.getItem(KEY)!))).toEqual(
        new Set(['x:2026-10-28T10:00:00Z', 'y:2026-10-28T10:00:00Z']),
      );
    });

    it('004-AC4: a prune keeps a served dismissal another tab stored after mount', () => {
      localStorage.setItem(KEY, JSON.stringify(['old:t']));
      show(banner({ id: 'old', updated_at: 't' }), banner({ id: 'x', message: 'x' }));
      render(<AnnouncementBanners />);
      localStorage.setItem(KEY, JSON.stringify(['old:t', 'x:2026-10-28T10:00:00Z'])); // tab A closes x
      act(() => show(banner({ id: 'x', message: 'x' }))); // 'old' withdrawn: prune runs
      expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(['x:2026-10-28T10:00:00Z']);
    });

    it('004-AC4: a close in another tab hides the banner here', () => {
      show(banner());
      render(<AnnouncementBanners />);
      localStorage.setItem(KEY, JSON.stringify(['a1:2026-10-28T10:00:00Z']));
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: 'unrelated' }));
      });
      expect(status()).toBeInTheDocument();
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: KEY }));
      });
      expect(screen.queryByRole('status')).toBeNull();
    });

    it('removes the storage listener on unmount', () => {
      const spy = vi.spyOn(window, 'removeEventListener');
      const { unmount } = render(<AnnouncementBanners />);
      unmount();
      expect(spy.mock.calls.map((c) => c[0])).toContain('storage');
    });

    it('renders when storage reads throw (blocked site data)', () => {
      vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
        throw new DOMException('denied', 'SecurityError');
      });
      show(banner());
      render(<AnnouncementBanners />);
      expect(status()).toHaveTextContent('Maintenance tonight');
    });
  });

  it('004-AC5: a non-dismissible banner has no close button', () => {
    show(banner({ dismissible: false }));
    render(<AnnouncementBanners />);
    expect(status()).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  describe('004-AC6: expiry', () => {
    it('leaves at ends_at with no refetch', async () => {
      vi.useFakeTimers({ now: new Date('2026-10-28T12:00:00Z') });
      show(banner({ ends_at: '2026-10-28T12:01:00Z' }));
      render(<AnnouncementBanners />);
      await act(() => vi.advanceTimersByTimeAsync(59_000));
      expect(status()).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(screen.queryByRole('status')).toBeNull();
      expect(refresh).toHaveBeenCalledTimes(1); // the mount one only
    });

    it('measures the delay from the current time, not from mount', async () => {
      vi.useFakeTimers({ now: new Date('2026-10-28T12:00:00Z') });
      show(banner({ id: 'x', ends_at: null }));
      render(<AnnouncementBanners />);
      await act(() => vi.advanceTimersByTimeAsync(3_600_000));
      act(() => show(banner({ id: 'late', message: 'late', ends_at: '2026-10-28T13:01:00Z' })));
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(screen.queryByRole('status')).toBeNull();
    });

    it('does not render a banner already past its ends_at', () => {
      show(banner({ ends_at: new Date(Date.now() - 1000).toISOString() }));
      const { container } = render(<AnnouncementBanners />);
      expect(container.firstChild).toBeNull();
    });

    it('clamps a far expiry to the setTimeout maximum instead of firing at once', async () => {
      vi.useFakeTimers({ now: new Date('2026-10-28T12:00:00Z') });
      const spy = vi.spyOn(globalThis, 'setTimeout');
      show(banner({ ends_at: '2027-01-28T12:00:00Z' })); // ~92 days, beyond 2^31-1 ms
      render(<AnnouncementBanners />);
      const delays = spy.mock.calls.map((c) => c[1] ?? 0);
      expect(delays.length).toBeGreaterThan(0);
      expect(Math.max(...delays)).toBeLessThanOrEqual(MAX_TIMEOUT_MS);
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(status()).toBeInTheDocument();
    });
  });

  describe('004-AC7: colors', () => {
    it.each([
      ['info', 'rgb(37, 99, 235)', 'white'],
      ['success', 'rgb(21, 128, 61)', 'white'],
      ['warning', 'rgb(245, 158, 11)', 'black'],
      ['danger', 'rgb(220, 38, 38)', 'white'],
    ] as const)('%s uses its level background and text', (level, bg, text) => {
      show(banner({ level }));
      render(<AnnouncementBanners />);
      expect(status().style.backgroundColor).toBe(bg);
      expect(status().style.color).toBe(text);
    });

    it('a custom background picks black or white text by luminance', () => {
      show(
        banner({ id: 'light', message: 'light', color: '#fef08a' }),
        banner({ id: 'dark', message: 'dark', color: '#1e1b4b' }),
      );
      render(<AnnouncementBanners />);
      const [light, dark] = screen.getAllByRole('status');
      expect(light.style.backgroundColor).toBe('rgb(254, 240, 138)');
      expect(light.style.color).toBe('black');
      expect(dark.style.color).toBe('white');
    });

    it('a custom color equal to a level color reads like that level', () => {
      show(
        banner({ id: 'w', message: 'w', level: 'info', color: '#f59e0b' }),
        banner({ id: 'i', message: 'i', level: 'warning', color: '#2563eb' }),
      );
      render(<AnnouncementBanners />);
      const [asWarning, asInfo] = screen.getAllByRole('status');
      expect(asWarning.style.color).toBe('black');
      expect(asInfo.style.color).toBe('white');
    });

    it('an explicit text color always wins', () => {
      show(
        banner({ id: 'c', message: 'c', color: '#fef08a', text_color: '#ff0000' }),
        banner({ id: 'l', message: 'l', level: 'warning', text_color: '#00ff00' }),
      );
      render(<AnnouncementBanners />);
      const [custom, level] = screen.getAllByRole('status');
      expect(custom.style.color).toBe('rgb(255, 0, 0)');
      expect(level.style.color).toBe('rgb(0, 255, 0)');
    });

    it('opacity below 100 renders a translucent background', () => {
      show(banner({ id: 't', message: 't', opacity: 40 }), banner({ id: 'o', message: 'o', opacity: 100 }));
      render(<AnnouncementBanners />);
      const [translucent, opaque] = screen.getAllByRole('status');
      expect(translucent.style.backgroundColor).toBe('rgba(37, 99, 235, 0.4)');
      expect(opaque.style.backgroundColor).toBe('rgb(37, 99, 235)');
    });

    // Independent of the component's helper: WCAG 2 contrast of the rendered text color against
    // the rendered background composited over the shell's bg-gray-900 (#111827).
    const SHELL = [0x11, 0x18, 0x27];
    const NAMED: Record<string, number[]> = { black: [0, 0, 0], white: [255, 255, 255] };
    const channel = (c: number) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    const luminance = ([r, g, b]: number[]) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    const drawnContrast = (el: HTMLElement) => {
      const [r, g, b, a = 1] = el.style.backgroundColor.match(/[\d.]+/g)!.map(Number);
      const bg = [r, g, b].map((c, i) => c * a + SHELL[i] * (1 - a));
      const fg = NAMED[el.style.color];
      const [hi, lo] = [luminance(bg), luminance(fg)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    const OPACITIES = [10, 30, 50, 75, 100];
    const CASES = [
      ...(['info', 'success', 'warning', 'danger'] as const).map((level) => ({ label: level, over: { level } })),
      ...['#fef08a', '#7c3aed', '#808080'].map((color) => ({ label: color, over: { color } })),
    ].flatMap(({ label, over }) => OPACITIES.map((opacity) => [label, opacity, over] as const));

    it.each(CASES)('auto text on %s at %i%% opacity reaches AA (>= 4.5:1) as drawn', (_label, opacity, over) => {
      show(banner({ ...over, opacity }));
      render(<AnnouncementBanners />);
      expect(drawnContrast(status())).toBeGreaterThanOrEqual(4.5);
    });
  });

  describe('004-AC8: inline markdown', () => {
    const renderMessage = (message: string) => {
      show(banner({ message }));
      render(<AnnouncementBanners />);
      return status();
    };

    it('renders bold, italic, inline code, strikethrough and links inline', () => {
      const el = renderMessage('**bold** *it* `code` ~~old~~ [docs](https://example.com/x)');
      expect(el.querySelector('strong')).toHaveTextContent('bold');
      expect(el.querySelector('em')).toHaveTextContent('it');
      expect(el.querySelector('code')).toHaveTextContent('code');
      expect(el.querySelector('del')).toHaveTextContent('old');
      expect(el.querySelector('p')).toBeNull();
      const link = screen.getByRole('link', { name: 'docs' });
      expect(link).toHaveAttribute('href', 'https://example.com/x');
      expect(link).toHaveAttribute('target', '_blank');
      expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    });

    it('keeps a mailto link', () => {
      renderMessage('[ops](mailto:ops@example.com)');
      expect(screen.getByRole('link', { name: 'ops' })).toHaveAttribute('href', 'mailto:ops@example.com');
    });

    it.each([
      ['javascript:', '[x](javascript:alert(1))'],
      ['JaVaScRiPt: mixed case', '[x](JaVaScRiPt:alert(1))'],
      ['data:', '[x](data:text/html,<script>alert(1)</script>)'],
      ['irc: (allowed by the default transform)', '[x](irc://irc.example.com/chan)'],
      ['a relative path', '[x](/admin/users)'],
    ])('neutralizes a %s link', (_label, message) => {
      const el = renderMessage(message);
      expect(screen.queryByRole('link')).toBeNull();
      expect(el.querySelector('a')).toBeNull();
      expect(el).toHaveTextContent('x');
    });

    it.each([
      ['inline', 'hi <img src=x onerror="alert(1)"> <script>alert(2)</script> <b>b</b>'],
      ['as an HTML block', '<script>alert(2)</script>\n\n<img src=x onerror="alert(1)">\n\n<b>b</b>'],
    ])('renders raw HTML %s as text, never as elements', (_label, message) => {
      const el = renderMessage(message);
      expect(el.querySelector('img')).toBeNull();
      expect(el.querySelector('script')).toBeNull();
      expect(el.querySelector('b')).toBeNull();
      expect(el.textContent).toContain('<img src=x onerror="alert(1)">');
      expect(el.textContent).toContain('<script>');
    });

    it('drops block markdown: headings, lists, quotes, images', () => {
      const el = renderMessage('# Title\n\n- one\n- two\n\n> quoted\n\n![alt](https://example.com/i.png)');
      for (const tag of ['h1', 'ul', 'li', 'blockquote', 'img', 'p']) {
        expect(el.querySelector(tag)).toBeNull();
      }
      expect(el).toHaveTextContent('Title');
      expect(el).toHaveTextContent('one');
      expect(el).toHaveTextContent('quoted');
    });
  });
});
