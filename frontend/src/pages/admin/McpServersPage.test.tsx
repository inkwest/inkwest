import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { McpServersPage } from './McpServersPage';
import * as adminApi from '@/services/adminApi';
import type { McpOauthServer } from '@/types/admin';

vi.mock('@/services/adminApi');

const NOTION: McpOauthServer = {
  key: 'notion',
  display_name: 'Notion',
  url: 'https://mcp.notion.com/mcp',
  scopes: ['mcp:read'],
  client_id: null,
  has_secret: false,
  editable: true,
  enabled: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  // The store is module-level; reset its list between tests via a fresh fetch result.
  vi.mocked(adminApi.getMcpOauthServers).mockResolvedValue([NOTION]);
  // Secret storage on by default so the Confidential auth type is enabled in tests.
  vi.mocked(adminApi.getMcpOauthMeta).mockResolvedValue({ secret_storage_enabled: true });
});

describe('McpServersPage', () => {
  it('lists configured servers', async () => {
    render(<McpServersPage />);
    expect(await screen.findByText('notion')).toBeInTheDocument();
    expect(screen.getByText('https://mcp.notion.com/mcp')).toBeInTheDocument();
    expect(screen.getByText('mcp:read')).toBeInTheDocument();
  });

  it('disables Edit for a server whose sealed secret cannot be decrypted', async () => {
    const locked: McpOauthServer = { ...NOTION, has_secret: true, editable: false };
    vi.mocked(adminApi.getMcpOauthServers).mockResolvedValue([locked]);
    render(<McpServersPage />);
    await screen.findByText('notion');
    expect(screen.getByRole('button', { name: /edit notion/i })).toBeDisabled();
    // Delete stays available so the operator can clean it up.
    expect(screen.getByRole('button', { name: /delete notion/i })).not.toBeDisabled();
  });

  it('creates a server through the form', async () => {
    const created: McpOauthServer = {
      ...NOTION,
      key: 'gh',
      display_name: null,
      url: 'https://gh.example.com/mcp',
      scopes: [],
    };
    vi.mocked(adminApi.createMcpOauthServer).mockResolvedValue([NOTION, created]);

    render(<McpServersPage />);
    await screen.findByText('notion');

    await userEvent.click(screen.getByRole('button', { name: /add mcp server/i }));
    await userEvent.type(screen.getByLabelText('Key'), 'gh');
    await userEvent.type(screen.getByLabelText('URL'), 'https://gh.example.com/mcp');
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }));

    await waitFor(() =>
      expect(adminApi.createMcpOauthServer).toHaveBeenCalledWith(
        expect.objectContaining({ key: 'gh', url: 'https://gh.example.com/mcp' }),
      ),
    );
  });

  it('confidential edit with blank secret omits client_secret (preserves stored one)', async () => {
    const confidential: McpOauthServer = {
      ...NOTION,
      client_id: 'cid',
      has_secret: true, // ⇒ derived auth type "confidential"
    };
    vi.mocked(adminApi.getMcpOauthServers).mockResolvedValue([confidential]);
    vi.mocked(adminApi.updateMcpOauthServer).mockResolvedValue([confidential]);

    render(<McpServersPage />);
    await screen.findByText('notion');

    await userEvent.click(screen.getByRole('button', { name: /edit notion/i }));
    // The Confidential segment is preselected; leave the secret blank and save.
    await userEvent.click(screen.getByRole('button', { name: /^update$/i }));

    await waitFor(() => expect(adminApi.updateMcpOauthServer).toHaveBeenCalled());
    const [, payload] = vi.mocked(adminApi.updateMcpOauthServer).mock.calls[0];
    expect(payload).not.toHaveProperty('client_secret'); // blank ⇒ keep stored
    expect(payload.client_id).toBe('cid');
  });

  it('shows the enable-instructions instead of inputs when secret storage is off', async () => {
    vi.mocked(adminApi.getMcpOauthMeta).mockResolvedValue({ secret_storage_enabled: false });
    render(<McpServersPage />);
    await screen.findByText('notion');
    await userEvent.click(screen.getByRole('button', { name: /add mcp server/i }));

    // Confidential is greyed but still clickable.
    const confidential = screen.getByRole('button', { name: /confidential/i });
    expect(confidential).not.toBeDisabled();
    await userEvent.click(confidential);

    // The copyable SECRETBOX_KEYS instructions replace the credential inputs.
    expect(screen.getAllByText(/SECRETBOX_KEYS/).length).toBeGreaterThan(0);
    expect(screen.getByText('openssl rand -base64 32')).toBeInTheDocument();
    expect(screen.queryByLabelText('Client Secret')).not.toBeInTheDocument();
    // …and there's nothing to submit.
    expect(screen.getByRole('button', { name: /^create$/i })).toBeDisabled();
  });

  it('blocks a duplicate key before calling the API', async () => {
    render(<McpServersPage />);
    await screen.findByText('notion');
    await userEvent.click(screen.getByRole('button', { name: /add mcp server/i }));
    await userEvent.type(screen.getByLabelText('Key'), 'notion'); // already exists
    await userEvent.type(screen.getByLabelText('URL'), 'https://x.example.com/mcp');
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
    expect(adminApi.createMcpOauthServer).not.toHaveBeenCalled();
  });

  it('flags a non-https URL on blur, before submit', async () => {
    render(<McpServersPage />);
    await screen.findByText('notion');
    await userEvent.click(screen.getByRole('button', { name: /add mcp server/i }));
    await userEvent.type(screen.getByLabelText('URL'), 'http://insecure/mcp');
    await userEvent.tab(); // blur the URL field
    expect(await screen.findByText(/must start with https/i)).toBeInTheDocument();
    expect(adminApi.createMcpOauthServer).not.toHaveBeenCalled();
  });

  it('blocks a non-https URL before calling the API', async () => {
    render(<McpServersPage />);
    await screen.findByText('notion');
    await userEvent.click(screen.getByRole('button', { name: /add mcp server/i }));
    await userEvent.type(screen.getByLabelText('Key'), 'gh');
    await userEvent.type(screen.getByLabelText('URL'), 'http://insecure/mcp');
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }));
    expect(await screen.findByText(/must start with https/i)).toBeInTheDocument();
    expect(adminApi.createMcpOauthServer).not.toHaveBeenCalled();
  });

  it('lets you edit an existing confidential server when storage is off', async () => {
    vi.mocked(adminApi.getMcpOauthMeta).mockResolvedValue({ secret_storage_enabled: false });
    const conf: McpOauthServer = { ...NOTION, client_id: 'cid', has_secret: true };
    vi.mocked(adminApi.getMcpOauthServers).mockResolvedValue([conf]);
    vi.mocked(adminApi.updateMcpOauthServer).mockResolvedValue([conf]);

    render(<McpServersPage />);
    await screen.findByText('notion');
    await userEvent.click(screen.getByRole('button', { name: /edit notion/i }));

    // Inputs are shown (not the enable-panel); the secret is locked to preserve-only.
    expect(screen.getByLabelText('Client ID')).toBeInTheDocument();
    expect(screen.getByLabelText('Client Secret')).toBeDisabled();
    expect(screen.getByRole('button', { name: /^update$/i })).not.toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: /^update$/i }));
    await waitFor(() => expect(adminApi.updateMcpOauthServer).toHaveBeenCalled());
    const [, payload] = vi.mocked(adminApi.updateMcpOauthServer).mock.calls[0];
    expect(payload).not.toHaveProperty('client_secret'); // preserved
    expect(payload.client_id).toBe('cid');
  });

  it('switching a confidential server to DCR clears id and secret', async () => {
    const confidential: McpOauthServer = {
      ...NOTION,
      client_id: 'cid',
      has_secret: true,
    };
    vi.mocked(adminApi.getMcpOauthServers).mockResolvedValue([confidential]);
    vi.mocked(adminApi.updateMcpOauthServer).mockResolvedValue([confidential]);

    render(<McpServersPage />);
    await screen.findByText('notion');

    await userEvent.click(screen.getByRole('button', { name: /edit notion/i }));
    await userEvent.click(screen.getByRole('button', { name: /dynamic \(dcr\)/i }));
    await userEvent.click(screen.getByRole('button', { name: /^update$/i }));

    await waitFor(() => expect(adminApi.updateMcpOauthServer).toHaveBeenCalled());
    const [, payload] = vi.mocked(adminApi.updateMcpOauthServer).mock.calls[0];
    expect(payload.client_id).toBeNull();
    expect(payload.client_secret).toBe(''); // "" explicitly clears the stored secret
  });
});
