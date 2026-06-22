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

  it('captures the build timestamp at baseline (display-only, not compared)', () => {
    useUpdateStore.getState().recordVersion('v0.0.0-9f6f96e', '2026-06-22T10:00:00Z');
    expect(useUpdateStore.getState().loadedBuildTimestamp).toBe('2026-06-22T10:00:00Z');
    // A later reading's timestamp must not matter — only the version drives the banner.
    useUpdateStore.getState().recordVersion('v0.0.0-9f6f96e', '2026-06-22T11:00:00Z');
    expect(useUpdateStore.getState().updateAvailable).toBe(false);
    expect(useUpdateStore.getState().loadedBuildTimestamp).toBe('2026-06-22T10:00:00Z');
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
