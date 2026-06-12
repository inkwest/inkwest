import { describe, it, expect, beforeEach } from 'vitest';
import { useUpdateStore } from './update';

describe('useUpdateStore', () => {
  beforeEach(() => {
    useUpdateStore.getState().reset();
  });

  it('takes the first reading as the baseline without flagging an update', () => {
    useUpdateStore.getState().recordVersion('v1');
    expect(useUpdateStore.getState().loadedVersion).toBe('v1');
    expect(useUpdateStore.getState().updateAvailable).toBe(false);
  });

  it('does not flag an update when the version is unchanged', () => {
    useUpdateStore.getState().recordVersion('v1');
    useUpdateStore.getState().recordVersion('v1');
    expect(useUpdateStore.getState().updateAvailable).toBe(false);
  });

  it('flags an update when a later reading differs from the baseline', () => {
    useUpdateStore.getState().recordVersion('v1');
    useUpdateStore.getState().recordVersion('v2');
    expect(useUpdateStore.getState().updateAvailable).toBe(true);
    // Baseline stays put — it is what the browser is running.
    expect(useUpdateStore.getState().loadedVersion).toBe('v1');
    // The newer version is captured for display in the banner.
    expect(useUpdateStore.getState().availableVersion).toBe('v2');
  });

  it('keeps the baseline so dismiss can re-surface on the next differing reading', () => {
    useUpdateStore.getState().recordVersion('v1');
    useUpdateStore.getState().recordVersion('v2');
    useUpdateStore.getState().dismiss();
    expect(useUpdateStore.getState().updateAvailable).toBe(false);

    useUpdateStore.getState().recordVersion('v3');
    expect(useUpdateStore.getState().updateAvailable).toBe(true);
  });
});
