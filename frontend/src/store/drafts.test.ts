import { describe, it, expect, beforeEach } from 'vitest';
import { useDraftsStore } from './drafts';

describe('draftsStore', () => {
  beforeEach(() => {
    useDraftsStore.setState({ drafts: {} });
  });

  it('starts empty', () => {
    expect(useDraftsStore.getState().drafts).toEqual({});
  });

  it('keeps drafts isolated per session', () => {
    const { setDraft } = useDraftsStore.getState();
    setDraft('a', 'draft for a');
    setDraft('b', 'draft for b');
    const { drafts } = useDraftsStore.getState();
    expect(drafts.a).toBe('draft for a');
    expect(drafts.b).toBe('draft for b');
  });

  it('overwrites an existing draft for the same session', () => {
    const { setDraft } = useDraftsStore.getState();
    setDraft('a', 'first');
    setDraft('a', 'second');
    expect(useDraftsStore.getState().drafts.a).toBe('second');
  });

  it('clears only the targeted session draft', () => {
    const { setDraft, clearDraft } = useDraftsStore.getState();
    setDraft('a', 'keep');
    setDraft('b', 'remove');
    clearDraft('b');
    const { drafts } = useDraftsStore.getState();
    expect(drafts.a).toBe('keep');
    expect('b' in drafts).toBe(false);
  });

  it('clearDraft on an unknown session is a no-op', () => {
    const { setDraft, clearDraft } = useDraftsStore.getState();
    setDraft('a', 'keep');
    clearDraft('does-not-exist');
    expect(useDraftsStore.getState().drafts).toEqual({ a: 'keep' });
  });
});
