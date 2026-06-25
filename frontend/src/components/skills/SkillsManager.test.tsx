import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SkillsManager, type ManagedSkill } from './SkillsManager';

const skills: ManagedSkill[] = [
  { id: '1', name: 'alpha', description: 'first', content: 'c', is_active: true },
  { id: '2', name: 'beta', description: 'second', content: 'c', is_active: false, is_override: true },
];

function setup(props: Partial<React.ComponentProps<typeof SkillsManager>> = {}) {
  const onCreate = vi.fn().mockResolvedValue(undefined);
  const onUpdate = vi.fn().mockResolvedValue(undefined);
  const onDelete = vi.fn().mockResolvedValue(undefined);
  const onOverride = vi.fn().mockResolvedValue(undefined);
  render(
    <SkillsManager
      skills={skills}
      onCreate={onCreate}
      onUpdate={onUpdate}
      onDelete={onDelete}
      onOverride={onOverride}
      {...props}
    />
  );
  return { onCreate, onUpdate, onDelete, onOverride };
}

describe('SkillsManager', () => {
  it('renders the skills', () => {
    setup();
    expect(screen.getByText('alpha')).toBeInTheDocument();
    expect(screen.getByText('beta')).toBeInTheDocument();
  });

  it('shows the override badge only when enabled', () => {
    setup({ showOverrideBadge: true });
    expect(screen.getByText('override')).toBeInTheDocument();
  });

  it('hides the override badge by default', () => {
    setup();
    expect(screen.queryByText('override')).not.toBeInTheDocument();
  });

  it('toggling status calls onUpdate with the inverted is_active', async () => {
    const user = userEvent.setup();
    const { onUpdate } = setup();
    await user.click(screen.getByText('Active')); // alpha is active → toggling sets false
    expect(onUpdate).toHaveBeenCalledWith('1', { is_active: false });
  });

  it('creating a skill submits its content', async () => {
    const user = userEvent.setup();
    const { onCreate } = setup();
    await user.click(screen.getByText('Add Skill'));
    const textarea = screen.getByLabelText('SKILL.md');
    await user.type(textarea, '---\nname: x\n---\nbody');
    await user.click(screen.getByText('Create'));
    expect(onCreate).toHaveBeenCalledWith('---\nname: x\n---\nbody');
  });

  it('offers override buttons for non-overridden admin skills', async () => {
    const user = userEvent.setup();
    const { onOverride } = setup({
      overridableAdmin: [
        { name: 'mr-review', alreadyOverridden: false },
        { name: 'already', alreadyOverridden: true },
      ],
    });
    expect(screen.getByText('mr-review')).toBeInTheDocument();
    expect(screen.queryByText('already')).not.toBeInTheDocument(); // already overridden → hidden
    await user.click(screen.getByText('mr-review'));
    expect(onOverride).toHaveBeenCalledWith('mr-review');
  });
});
