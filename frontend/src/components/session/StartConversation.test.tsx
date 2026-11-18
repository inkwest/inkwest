import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { StartConversation } from './StartConversation';
import { useSessionsStore } from '@/store/sessions';
import { useComposerStore } from '@/store/composer';
import { useConfigStore } from '@/store/config';
import { useSkillsStore } from '@/store/skills';
import { useAuthStore } from '@/store/admin';
import { captureLinkPrefill } from '@/lib/linkPrefill';

// Stub the connector/MCP/tour/model children: they fetch on mount and are not under test.
vi.mock('@/components/onboarding/OnboardingTour', () => ({ OnboardingTour: () => null }));
vi.mock('@/components/connector/ConnectorPanel', () => ({ ConnectorPanel: () => null }));
vi.mock('@/components/connector/McpPanel', () => ({ McpPanel: () => null }));
vi.mock('@/components/chat/ModelEnginePicker', () => ({ ModelEnginePicker: () => null }));

const NOTICE = 'Prefilled from a link. Review it before sending.';
const createSession = vi.fn();
const setPendingPrompt = vi.fn();

function openLink(text: string) {
  window.history.replaceState(null, '', `/#prompt=${encodeURIComponent(text)}`);
  captureLinkPrefill();
}

function renderStart() {
  return render(
    <StrictMode>
      <MemoryRouter>
        <StartConversation />
      </MemoryRouter>
    </StrictMode>,
  );
}

const textarea = () =>
  screen.getByPlaceholderText(/investigate or analyse/i) as HTMLTextAreaElement;

describe('StartConversation link prefill', () => {
  beforeEach(() => {
    sessionStorage.clear();
    window.history.replaceState(null, '', '/');
    createSession.mockReset().mockResolvedValue({ id: 's1' });
    setPendingPrompt.mockReset();
    useSessionsStore.setState({ createSession, setPendingPrompt });
    useComposerStore.setState({ pending: null, nonce: 0 });
    useConfigStore.setState({ config: null, fetchConfig: vi.fn() });
    useSkillsStore.setState({ templates: [], fetchTemplates: vi.fn(), isLoading: false, error: null });
    useAuthStore.setState({ user: null });
  });

  it('005-AC1 / 005-AC7: fills the composer and shows the notice under StrictMode', () => {
    const text = 'why is\n& # + % 🚀 <b>pod</b> crashing?';
    openLink(text);
    renderStart();
    expect(textarea().value).toBe(text);
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
  });

  it('005-AC3: a prefill creates no session and sends nothing', async () => {
    openLink('do not send me');
    renderStart();
    expect(textarea().value).toBe('do not send me');
    // Let any stray async effect settle before asserting nothing fired.
    await new Promise((r) => setTimeout(r, 50));
    expect(createSession).not.toHaveBeenCalled();
    expect(setPendingPrompt).not.toHaveBeenCalled();
  });

  it('005-AC7: dismiss hides the notice and keeps the text', async () => {
    const user = userEvent.setup();
    openLink('keep me');
    renderStart();
    await user.click(screen.getByRole('button', { name: /dismiss prefill notice/i }));
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
    expect(textarea().value).toBe('keep me');
  });

  it('005-AC7: send hides the notice', async () => {
    const user = userEvent.setup();
    openLink('send me');
    renderStart();
    await user.click(screen.getByRole('button', { name: /send message/i }));
    // No connector granted on the start screen: confirm through the "Send anyway" modal.
    await user.click(screen.getByRole('button', { name: /send anyway/i }));
    await waitFor(() => expect(setPendingPrompt).toHaveBeenCalledWith('send me', null));
    expect(createSession).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });

  it('005-AC1 / 005-AC7: a link opened in a tab already on / prefills on hashchange', () => {
    renderStart();
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
    const text = 'same-tab\n& # 🚀';
    // Same-document navigation: the URL changes and only hashchange fires, no reboot.
    window.history.replaceState(null, '', `/#a=1&prompt=${encodeURIComponent(text)}`);
    act(() => {
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(textarea().value).toBe(text);
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    expect(window.location.hash).toBe('#a=1');
    expect(createSession).not.toHaveBeenCalled();
  });

  it('005-AC7: a hashchange without prompt does nothing', () => {
    renderStart();
    window.history.replaceState(null, '', '/#a=1');
    act(() => {
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(textarea().value).toBe('');
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
    expect(window.location.hash).toBe('#a=1');
  });

  it('005-AC7: no prefill, no notice', () => {
    renderStart();
    expect(textarea().value).toBe('');
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });
});
