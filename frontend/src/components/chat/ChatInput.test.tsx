import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatInput } from './ChatInput';
import { useDraftsStore } from '@/store/drafts';

describe('ChatInput', () => {
  const onSend = vi.fn();

  beforeEach(() => {
    onSend.mockClear();
    useDraftsStore.setState({ drafts: {} });
  });

  it('renders textarea and send button', () => {
    render(<ChatInput onSend={onSend} />);
    expect(screen.getByPlaceholderText(/investigate or analyse/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /send message/i })).toBeInTheDocument();
  });

  it('disables inputs when disabled prop is true', () => {
    render(<ChatInput onSend={onSend} disabled />);
    expect(screen.getByPlaceholderText(/investigate or analyse/i)).toBeDisabled();
    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled();
  });

  it('calls onSend when send button is clicked', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'list pods');
    await user.click(screen.getByRole('button', { name: /send message/i }));

    expect(onSend).toHaveBeenCalledWith('list pods');
  });

  it('calls onSend on Enter key', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'get nodes{enter}');

    expect(onSend).toHaveBeenCalledWith('get nodes');
  });

  it('does not send on Shift+Enter', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i);
    await user.type(textarea, 'line1{shift>}{enter}{/shift}line2');

    expect(onSend).not.toHaveBeenCalled();
  });

  it('does not send empty messages', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    await user.click(screen.getByRole('button', { name: /send message/i }));
    expect(onSend).not.toHaveBeenCalled();
  });

  it('clears input after sending', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} />);

    const textarea = screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;
    await user.type(textarea, 'message{enter}');

    expect(textarea.value).toBe('');
  });

  it('populates initial prompt', () => {
    render(<ChatInput onSend={onSend} initialPrompt="check pods" />);
    const textarea = screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;
    expect(textarea.value).toBe('check pods');
  });

  it('send button is disabled when textarea is empty', () => {
    render(<ChatInput onSend={onSend} />);
    expect(screen.getByRole('button', { name: /send message/i })).toBeDisabled();
  });

  it('restores a per-session draft after unmount/remount', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ChatInput onSend={onSend} sessionId="s1" />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'investigate crashloop');
    unmount();

    render(<ChatInput onSend={onSend} sessionId="s1" />);
    expect((screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement).value).toBe(
      'investigate crashloop'
    );
  });

  it('does not leak a draft into a different session', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ChatInput onSend={onSend} sessionId="s1" />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'only for s1');
    unmount();

    render(<ChatInput onSend={onSend} sessionId="s2" />);
    expect((screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement).value).toBe('');
  });

  it('clears the saved draft after sending', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ChatInput onSend={onSend} sessionId="s1" />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'send me{enter}');
    expect(onSend).toHaveBeenCalledWith('send me');
    unmount();

    render(<ChatInput onSend={onSend} sessionId="s1" />);
    expect((screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement).value).toBe('');
  });

  it('does not send on Enter while a response is streaming', async () => {
    const user = userEvent.setup();
    render(<ChatInput onSend={onSend} isStreaming />);
    await user.type(screen.getByPlaceholderText(/investigate or analyse/i), 'second message{enter}');
    expect(onSend).not.toHaveBeenCalled();
  });
});
