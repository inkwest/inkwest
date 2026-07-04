import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { usePreferencesStore } from './preferences';
import { useSettingsStore } from './settings';
import * as api from '@/services/api';
import type { PreferencesResponse, UserSkill } from '@/types/api';

function skill(name: string, over: Partial<UserSkill> = {}): UserSkill {
  return {
    id: name,
    name,
    slug: name,
    content: `---\nname: ${name}\n---\nb\n`,
    description: null,
    is_active: true,
    is_override: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  };
}

function resp(over: Partial<PreferencesResponse> = {}): PreferencesResponse {
  return { schema_version: 1, document: {}, skills: [], ...over };
}

describe('preferencesStore', () => {
  beforeEach(() => {
    usePreferencesStore.setState({
      loaded: false,
      loading: false,
      error: null,
      schemaVersion: 1,
      document: {},
      skills: [],
      disabledAdmin: [],
    });
    useSettingsStore.setState({
      compactToolCards: true,
      showTimestamps: true,
      wideComposer: false,
      sidebarWidth: 256,
    });
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('hydrates the UI cache from a server document', async () => {
    vi.spyOn(api, 'getMyPreferences').mockResolvedValue(
      resp({
        document: { ui: { compactToolCards: false, wideComposer: true } },
        skills: [skill('alpha')],
      })
    );
    const put = vi.spyOn(api, 'updateMyPreferences');

    await usePreferencesStore.getState().load();

    expect(useSettingsStore.getState().compactToolCards).toBe(false);
    expect(useSettingsStore.getState().wideComposer).toBe(true);
    expect(usePreferencesStore.getState().skills).toHaveLength(1);
    expect(put).not.toHaveBeenCalled(); // server already had UI → no migration
  });

  it('migrates the local cache to the server when the server UI is empty', async () => {
    useSettingsStore.setState({ compactToolCards: false });
    vi.spyOn(api, 'getMyPreferences').mockResolvedValue(resp({ document: {} }));
    const put = vi.spyOn(api, 'updateMyPreferences').mockResolvedValue(resp());

    await usePreferencesStore.getState().load();

    expect(put).toHaveBeenCalledOnce();
    const pushed = put.mock.calls[0][0];
    expect(pushed.ui?.compactToolCards).toBe(false); // local value seeded to the server
  });

  it('saveUi updates the cache and persists', async () => {
    const put = vi.spyOn(api, 'updateMyPreferences').mockResolvedValue(resp());
    await usePreferencesStore.getState().saveUi({ showTimestamps: false });
    expect(useSettingsStore.getState().showTimestamps).toBe(false);
    expect(put.mock.calls[0][0].ui?.showTimestamps).toBe(false);
  });

  it('round-trips showTokenCounts through saveUi', async () => {
    const put = vi.spyOn(api, 'updateMyPreferences').mockResolvedValue(resp());
    await usePreferencesStore.getState().saveUi({ showTokenCounts: false });
    expect(useSettingsStore.getState().showTokenCounts).toBe(false);
    expect(put.mock.calls[0][0].ui?.showTokenCounts).toBe(false);
  });

  it('setAdminSkillEnabled toggles the disabled set', async () => {
    const put = vi.spyOn(api, 'updateMyPreferences').mockResolvedValue(resp());
    await usePreferencesStore.getState().setAdminSkillEnabled('foo', false);
    expect(usePreferencesStore.getState().disabledAdmin).toContain('foo');
    expect(put.mock.calls[0][0].skills?.disabled_admin).toContain('foo');

    await usePreferencesStore.getState().setAdminSkillEnabled('foo', true);
    expect(usePreferencesStore.getState().disabledAdmin).not.toContain('foo');
  });

  it('createSkill appends and keeps the list name-sorted', async () => {
    usePreferencesStore.setState({ skills: [skill('beta')] });
    vi.spyOn(api, 'createMySkill').mockResolvedValue(skill('alpha'));
    await usePreferencesStore.getState().createSkill('---\nname: alpha\n---\nb');
    expect(usePreferencesStore.getState().skills.map((s) => s.name)).toEqual(['alpha', 'beta']);
  });

  it('deleteSkill removes the row', async () => {
    usePreferencesStore.setState({ skills: [skill('a'), skill('b')] });
    vi.spyOn(api, 'deleteMySkill').mockResolvedValue(undefined);
    await usePreferencesStore.getState().deleteSkill('a');
    expect(usePreferencesStore.getState().skills.map((s) => s.name)).toEqual(['b']);
  });

  it('importConfig replaces store state from the response', async () => {
    vi.spyOn(api, 'importMyConfig').mockResolvedValue(
      resp({
        document: { ui: { wideComposer: true }, skills: { disabled_admin: ['x'] } },
        skills: [skill('imported')],
      })
    );
    await usePreferencesStore
      .getState()
      .importConfig(
        { schema_version: 1, exported_at: '2026-01-01T00:00:00Z', preferences: {}, skills: [] },
        true
      );
    expect(usePreferencesStore.getState().skills.map((s) => s.name)).toEqual(['imported']);
    expect(usePreferencesStore.getState().disabledAdmin).toEqual(['x']);
    expect(useSettingsStore.getState().wideComposer).toBe(true);
  });

  it('surfaces an error when a save fails', async () => {
    vi.spyOn(api, 'updateMyPreferences').mockRejectedValue(new Error('boom'));
    await expect(usePreferencesStore.getState().saveUi({ wideComposer: true })).rejects.toThrow();
    expect(usePreferencesStore.getState().error).toBe('boom');
  });
});
