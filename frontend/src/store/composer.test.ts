import { describe, it, expect, beforeEach } from 'vitest';
import { useComposerStore } from './composer';

describe('composerStore', () => {
  beforeEach(() => {
    useComposerStore.setState({ pending: null, nonce: 0 });
  });

  it('starts with an empty channel', () => {
    const { pending, nonce } = useComposerStore.getState();
    expect(pending).toBeNull();
    expect(nonce).toBe(0);
  });

  it('inject stores the text and bumps the nonce', () => {
    useComposerStore.getState().inject('reuse me');
    const { pending, nonce } = useComposerStore.getState();
    expect(pending).toBe('reuse me');
    expect(nonce).toBe(1);
  });

  it('bumps the nonce even when injecting identical text', () => {
    const { inject } = useComposerStore.getState();
    inject('same');
    inject('same');
    expect(useComposerStore.getState().nonce).toBe(2);
    expect(useComposerStore.getState().pending).toBe('same');
  });

  it('consume clears pending but leaves the nonce intact', () => {
    useComposerStore.getState().inject('text');
    useComposerStore.getState().consume();
    const { pending, nonce } = useComposerStore.getState();
    expect(pending).toBeNull();
    expect(nonce).toBe(1);
  });
});
