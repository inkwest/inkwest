import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Outlet } from 'react-router-dom';
import App from './App';
import { useAuthStore } from '@/store/admin';
import { useSessionsStore } from '@/store/sessions';
import { useConfigStore } from '@/store/config';
import { useSkillsStore } from '@/store/skills';
import { captureLinkPrefill } from '@/lib/linkPrefill';

// Stub the shell and the start page's fetching children: only the route tree is under test.
vi.mock('@/components/layout/Layout', () => ({ Layout: () => <Outlet /> }));
vi.mock('@/components/onboarding/OnboardingTour', () => ({ OnboardingTour: () => null }));
vi.mock('@/components/connector/ConnectorPanel', () => ({ ConnectorPanel: () => null }));
vi.mock('@/components/connector/McpPanel', () => ({ McpPanel: () => null }));
vi.mock('@/components/chat/ModelEnginePicker', () => ({ ModelEnginePicker: () => null }));

describe('App routes', () => {
  const createSession = vi.fn();
  const setPendingPrompt = vi.fn();

  beforeEach(() => {
    createSession.mockReset();
    setPendingPrompt.mockReset();
    useAuthStore.setState({ isAuthenticated: true, isLoading: false, oidcEnabled: false, checkAuth: vi.fn() });
    useSessionsStore.setState({ createSession, setPendingPrompt });
    useConfigStore.setState({ config: null, fetchConfig: vi.fn() });
    useSkillsStore.setState({ templates: [], fetchTemplates: vi.fn(), isLoading: false, error: null });
  });

  it('005-AC8: a legacy /start link lands on an empty start page and sends nothing', async () => {
    window.history.replaceState(null, '', '/start?cluster=prod&prompt=legacy');
    render(<App />);
    const textarea = (await screen.findByPlaceholderText(/investigate or analyse/i)) as HTMLTextAreaElement;
    expect(window.location.pathname).toBe('/');
    expect(window.location.search).toBe('');
    expect(textarea.value).toBe('');
    expect(createSession).not.toHaveBeenCalled();
    expect(setPendingPrompt).not.toHaveBeenCalled();
  });

  it('005-AC8: a legacy #cluster=&q= link on the index route neither prefills nor sends', async () => {
    window.history.replaceState(null, '', '/#cluster=prod&q=legacy');
    captureLinkPrefill(); // as main.tsx does at boot
    render(<App />);
    const textarea = (await screen.findByPlaceholderText(/investigate or analyse/i)) as HTMLTextAreaElement;
    await new Promise((r) => setTimeout(r, 50));
    expect(textarea.value).toBe('');
    expect(screen.queryByText(/Prefilled from a link/)).not.toBeInTheDocument();
    expect(createSession).not.toHaveBeenCalled();
    expect(setPendingPrompt).not.toHaveBeenCalled();
  });
});
