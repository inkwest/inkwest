import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ChatInput } from './ChatInput';
import { useDraftsStore } from '@/store/drafts';
import { useChatStore } from '@/store/chat';
import { useSkillsStore } from '@/store/skills';
import { useConfigStore } from '@/store/config';
import * as api from '@/services/api';

// Keep the rest of the api module real; stub only the attachment calls.
vi.mock('@/services/api', async (importActual) => ({
  ...(await importActual<typeof import('@/services/api')>()),
  uploadAttachments: vi.fn(),
  deleteAttachment: vi.fn(),
}));

const NOTE = { filename: 'note.txt', original_name: 'note.txt', content_type: 'text/plain', size: 5 };

function enableAttachments(enabled: boolean) {
  useConfigStore.setState({
    config: {
      provider: 'claude-cli',
      model: 'm',
      default_engine: 'claude-cli',
      engines: ['claude-cli'],
      available_models: [],
      attachments_enabled: enabled,
    },
  });
}

describe('ChatInput attachments', () => {
  const onSend = vi.fn();

  beforeEach(() => {
    onSend.mockClear();
    vi.mocked(api.uploadAttachments).mockReset();
    vi.mocked(api.deleteAttachment).mockReset().mockResolvedValue(undefined);
    useDraftsStore.setState({ drafts: {} });
    useChatStore.setState({ messages: [] });
    useSkillsStore.setState({ templates: [], fetchTemplates: vi.fn(), isLoading: false, error: null });
    enableAttachments(true);
    // jsdom doesn't implement object URLs; the image-preview path needs them.
    URL.createObjectURL = vi.fn(() => 'blob:mock');
    URL.revokeObjectURL = vi.fn();
  });

  it('hides the attach button when the feature is disabled', () => {
    enableAttachments(false);
    render(<ChatInput onSend={onSend} sessionId="s1" />);
    expect(screen.queryByRole('button', { name: /attach files/i })).not.toBeInTheDocument();
  });

  it('hides the attach button when there is no session', () => {
    render(<ChatInput onSend={onSend} />);
    expect(screen.queryByRole('button', { name: /attach files/i })).not.toBeInTheDocument();
  });

  it('uploads a browsed file and shows a chip', async () => {
    vi.mocked(api.uploadAttachments).mockResolvedValue([NOTE]);
    const { container } = render(<ChatInput onSend={onSend} sessionId="s1" />);

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(['hello'], 'note.txt', { type: 'text/plain' }));

    await waitFor(() => expect(api.uploadAttachments).toHaveBeenCalledWith('s1', expect.any(Array)));
    expect(await screen.findByText('note.txt')).toBeInTheDocument();
  });

  it('removes a chip and calls deleteAttachment', async () => {
    vi.mocked(api.uploadAttachments).mockResolvedValue([NOTE]);
    const { container } = render(<ChatInput onSend={onSend} sessionId="s1" />);

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(['hello'], 'note.txt', { type: 'text/plain' }));
    await screen.findByText('note.txt');

    await userEvent.click(screen.getByRole('button', { name: /remove note\.txt/i }));
    expect(api.deleteAttachment).toHaveBeenCalledWith('s1', 'note.txt');
    expect(screen.queryByText('note.txt')).not.toBeInTheDocument();
  });

  it('sends the attachment descriptors (without previewUrl) on submit', async () => {
    vi.mocked(api.uploadAttachments).mockResolvedValue([NOTE]);
    const { container } = render(<ChatInput onSend={onSend} sessionId="s1" />);

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(['hello'], 'note.txt', { type: 'text/plain' }));
    await screen.findByText('note.txt');

    await userEvent.click(screen.getByRole('button', { name: /send message/i }));
    expect(onSend).toHaveBeenCalledWith('', [NOTE]);
  });

  it('uploads a pasted file', async () => {
    vi.mocked(api.uploadAttachments).mockResolvedValue([NOTE]);
    render(<ChatInput onSend={onSend} sessionId="s1" />);

    const file = new File(['hello'], 'note.txt', { type: 'text/plain' });
    fireEvent.paste(screen.getByPlaceholderText(/investigate or analyse/i), {
      clipboardData: { files: [file], getData: () => '' },
    });

    await waitFor(() => expect(api.uploadAttachments).toHaveBeenCalledWith('s1', [file]));
  });
});
