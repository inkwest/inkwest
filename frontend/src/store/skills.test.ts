import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as api from '@/services/api';
import { useSkillsStore } from './skills';
import type { Skill } from '@/types/api';

const PERSONAL: Skill = {
  id: '1',
  name: 'my-skill',
  slug: 'my-skill',
  content: '---\nname: my-skill\n---\nbody\n',
  description: 'Mine',
  is_active: true,
  created_at: '2026-08-13T00:00:00+00:00',
};

describe('useSkillsStore', () => {
  beforeEach(() => {
    useSkillsStore.setState({ templates: [], isLoading: false, error: null });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // Regression: the picker used to read the admin catalog, so a user's personal skill
  // never appeared in it even though the sandbox had materialized it.
  it('loads the effective set, not the admin catalog', async () => {
    const effective = vi.spyOn(api, 'getEffectiveSkills').mockResolvedValue([PERSONAL]);
    const admin = vi.spyOn(api, 'getSkills').mockResolvedValue([]);

    await useSkillsStore.getState().fetchTemplates();

    expect(effective).toHaveBeenCalled();
    expect(admin).not.toHaveBeenCalled();
    const s = useSkillsStore.getState();
    expect(s.templates).toEqual([PERSONAL]);
    expect(s.isLoading).toBe(false);
    expect(s.error).toBeNull();
  });

  it('an older fetch resolving after a newer one cannot clobber it', async () => {
    // Two Settings writes in quick succession fire two refreshes; the first response
    // (older data) arrives last and must be discarded.
    let resolveFirst!: (v: Skill[]) => void;
    const first = new Promise<Skill[]>((r) => (resolveFirst = r));
    vi.spyOn(api, 'getEffectiveSkills')
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce([PERSONAL]);

    const older = useSkillsStore.getState().fetchTemplates(); // hangs on `first`
    await useSkillsStore.getState().fetchTemplates(); // newer, resolves immediately
    expect(useSkillsStore.getState().templates).toEqual([PERSONAL]);

    resolveFirst([]); // older snapshot lands last…
    await older;
    expect(useSkillsStore.getState().templates).toEqual([PERSONAL]); // …and is discarded
  });

  it('records the error but keeps the last-good list when the fetch fails', async () => {
    // A background refresh (after a Settings write) must not blank the picker on a
    // transient failure — the skills are still triggerable (the sandbox reads the DB).
    useSkillsStore.setState({ templates: [PERSONAL] });
    vi.spyOn(api, 'getEffectiveSkills').mockRejectedValue(new Error('boom'));

    await useSkillsStore.getState().fetchTemplates();

    const s = useSkillsStore.getState();
    expect(s.templates).toEqual([PERSONAL]);
    expect(s.error).toBe('boom');
    expect(s.isLoading).toBe(false);
  });
});
