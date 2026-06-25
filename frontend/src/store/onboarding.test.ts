import { describe, it, expect, beforeEach } from 'vitest';
import { useOnboardingStore } from './onboarding';

describe('onboardingStore', () => {
  beforeEach(() => {
    useOnboardingStore.getState().restart();
  });

  it('defaults to not completed', () => {
    expect(useOnboardingStore.getState().completed).toBe(false);
  });

  it('complete() marks the tour seen', () => {
    useOnboardingStore.getState().complete();
    expect(useOnboardingStore.getState().completed).toBe(true);
  });

  it('restart() replays the tour', () => {
    useOnboardingStore.getState().complete();
    useOnboardingStore.getState().restart();
    expect(useOnboardingStore.getState().completed).toBe(false);
  });
});
