import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, act, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { SharedSessionPage } from './SharedSessionPage';
import { useChatStore } from '@/store/chat';

vi.mock('@/hooks/useSharedSocket', () => ({
  useSharedSocket: () => ({ status: 'ready', connection: 'live', meta: null }),
}));
vi.mock('@/components/chat/MessageList', () => ({
  MessageList: ({ messages }: { messages: unknown[] }) => <div>{messages.length}</div>,
}));
vi.mock('@/components/chat/StreamingMessage', () => ({ StreamingMessage: () => null }));

const msg = (id: string) => ({ id, session_id: '', role: 'user' as const, content: id, created_at: '' });

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/shared/T']}>
      <Routes>
        <Route path="/shared/:token" element={<SharedSessionPage />} />
      </Routes>
    </MemoryRouter>
  );
}

// jsdom has no layout: geometry is stubbed per scenario on the scroll container.
function geometry(el: HTMLElement, g: { scrollTop: number; scrollHeight: number; clientHeight: number }) {
  for (const [k, v] of Object.entries(g)) Object.defineProperty(el, k, { value: v, configurable: true });
}

describe('SharedSessionPage tail-follow', () => {
  const scrollIntoView = vi.fn();
  beforeEach(() => {
    useChatStore.setState({ messages: [] });
    Element.prototype.scrollIntoView = scrollIntoView;
    scrollIntoView.mockClear();
  });
  afterEach(() => useChatStore.setState({ messages: [] }));

  it('follows the tail when a tall message lands while the guest is at the bottom', () => {
    renderPage();
    const el = document.querySelector('.overflow-y-auto') as HTMLElement;
    geometry(el, { scrollTop: 500, scrollHeight: 1000, clientHeight: 500 });
    fireEvent.scroll(el); // sampled at the bottom
    scrollIntoView.mockClear();
    // The appended turn is taller than the threshold: the post-commit distance to the
    // bottom is 600px, which a measurement taken now would misread as "scrolled away".
    geometry(el, { scrollTop: 500, scrollHeight: 1600, clientHeight: 500 });
    act(() => useChatStore.setState({ messages: [msg('u1'), msg('tall')] }));
    expect(scrollIntoView).toHaveBeenCalled();
  });

  it('leaves a guest reading higher up alone when the transcript is re-read', () => {
    renderPage();
    const el = document.querySelector('.overflow-y-auto') as HTMLElement;
    geometry(el, { scrollTop: 0, scrollHeight: 1000, clientHeight: 500 });
    fireEvent.scroll(el); // sampled far from the bottom
    scrollIntoView.mockClear();
    act(() => useChatStore.setState({ messages: [msg('u1'), msg('u2')] }));
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
