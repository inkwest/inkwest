import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';
import { OnboardingTour } from './OnboardingTour';
import { useAuthStore } from '@/store/admin';
import { useOnboardingStore } from '@/store/onboarding';

function setUser(isAdmin: boolean | null) {
  useAuthStore.setState({
    user: isAdmin === null ? null : ({ id: 'u', email: 'e', is_admin: isAdmin } as never),
  });
}

// The live affordances the tour points at — present so steps spotlight them instead of
// falling back to a centred bubble (jsdom rects are zero, but the elements must exist).
function Anchors() {
  return (
    <>
      <button data-tour="attach">attach</button>
      <button data-tour="skills">skills</button>
      <span data-tour="connectors">
        <button>aws</button>
        <button>gitlab</button>
      </span>
      <button data-tour="mcp">mcp</button>
      <button data-tour="model">model</button>
    </>
  );
}

describe('OnboardingTour', () => {
  beforeEach(() => {
    useOnboardingStore.getState().restart();
    setUser(false);
  });
  afterEach(() => {
    cleanup(); // unmount before mutating the store, so restart() doesn't update a live tree
    useOnboardingStore.getState().restart();
  });

  it('shows for an admin too', () => {
    setUser(true);
    render(<><Anchors /><OnboardingTour /></>);
    expect(screen.getByText('Attachments')).toBeInTheDocument();
  });

  it('does not show once the tour was completed', () => {
    useOnboardingStore.getState().complete();
    render(<><Anchors /><OnboardingTour /></>);
    expect(screen.queryByText('Attachments')).not.toBeInTheDocument();
  });

  it('does not show before the user is known (auth unresolved)', () => {
    setUser(null);
    render(<><Anchors /><OnboardingTour /></>);
    expect(screen.queryByText('Attachments')).not.toBeInTheDocument();
  });

  it('walks a non-admin through the five steps and completes on Done', () => {
    render(<><Anchors /><OnboardingTour /></>);
    expect(screen.getByText('Attachments')).toBeInTheDocument();
    expect(screen.getByText('Step 1/5')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Next'));
    expect(screen.getByText('Skills')).toBeInTheDocument(); // exact: step title, not the "skills" anchor

    fireEvent.click(screen.getByText('Next'));
    expect(screen.getByText('Connectors')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Next'));
    expect(screen.getByText('MCP servers')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Next'));
    expect(screen.getByText('Model')).toBeInTheDocument();
    expect(screen.getByText('Done')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Done'));
    expect(useOnboardingStore.getState().completed).toBe(true);
    expect(screen.queryByText('Model')).not.toBeInTheDocument();
  });

  it('Enter advances through the steps and completes on the last one', () => {
    render(<><Anchors /><OnboardingTour /></>);
    expect(screen.getByText('Attachments')).toBeInTheDocument();

    for (const title of ['Skills', 'Connectors', 'MCP servers', 'Model']) {
      fireEvent.keyDown(window, { key: 'Enter' });
      expect(screen.getByText(title)).toBeInTheDocument();
    }

    fireEvent.keyDown(window, { key: 'Enter' }); // last step → Done
    expect(useOnboardingStore.getState().completed).toBe(true);
    expect(screen.queryByText('Model')).not.toBeInTheDocument();
  });

  it('does not hijack Enter while a text field is focused (the field owns it)', () => {
    render(<><Anchors /><input data-testid="field" /><OnboardingTour /></>);
    (screen.getByTestId('field') as HTMLInputElement).focus();
    fireEvent.keyDown(window, { key: 'Enter' });
    // The composer/input keeps its Enter — the tour must not advance past step 1.
    expect(screen.getByText('Step 1/5')).toBeInTheDocument();
    expect(screen.getByText('Attachments')).toBeInTheDocument();
  });

  it('does not hijack Enter when focus is inside an app modal (role="dialog")', () => {
    render(
      <>
        <Anchors />
        <div role="dialog">
          <button data-testid="modal-ok">ok</button>
        </div>
        <OnboardingTour />
      </>,
    );
    // e.g. the no-connector confirm's autofocused "Send anyway" button: the modal owns
    // Enter, the tour must not eat it (nor advance).
    screen.getByTestId('modal-ok').focus();
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(screen.getByText('Step 1/5')).toBeInTheDocument();
  });

  it('a held Enter (repeat) does not blast through the tour', () => {
    render(<><Anchors /><OnboardingTour /></>);
    fireEvent.keyDown(window, { key: 'Enter', repeat: true });
    expect(screen.getByText('Attachments')).toBeInTheDocument(); // still step 1
  });

  it('Enter does not hijack a focused bubble button (guard bows out)', () => {
    render(<><Anchors /><OnboardingTour /></>);
    fireEvent.click(screen.getByText('Next')); // step 2 → Back is now present
    screen.getByText('Back').focus();
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(screen.getByText('Skills')).toBeInTheDocument(); // did not advance to Connectors
  });

  it('Back steps to the previous tip', () => {
    render(<><Anchors /><OnboardingTour /></>);
    fireEvent.click(screen.getByText('Next'));
    expect(screen.getByText('Skills')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Back'));
    expect(screen.getByText('Attachments')).toBeInTheDocument();
  });

  it('Skip dismisses the tour and marks it completed', () => {
    render(<><Anchors /><OnboardingTour /></>);
    fireEvent.click(screen.getByText('Skip'));
    expect(useOnboardingStore.getState().completed).toBe(true);
    expect(screen.queryByText('Attachments')).not.toBeInTheDocument();
  });

  it('replaying (restart) resumes at step 1, not where it was left off', () => {
    render(<><Anchors /><OnboardingTour /></>);
    fireEvent.click(screen.getByText('Next')); // advance to step 2
    expect(screen.getByText('Skills')).toBeInTheDocument();

    act(() => useOnboardingStore.getState().complete()); // finish → tour hidden
    expect(screen.queryByText('Skills')).not.toBeInTheDocument();

    act(() => useOnboardingStore.getState().restart()); // "Replay tutorial"
    expect(screen.getByText('Attachments')).toBeInTheDocument();
    expect(screen.getByText('Step 1/5')).toBeInTheDocument();
  });
});
