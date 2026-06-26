import { Fragment, useCallback, useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Plus, Edit2, Trash2, ChevronRight, ChevronDown } from 'lucide-react';
import {
  Button,
  Card,
  CardContent,
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
  Modal,
  Input,
  Select,
} from '@/components/ui';
import { useBudgetStore, useUserStore } from '@/store/admin';
import {
  Budget,
  BudgetSettings,
  BudgetType,
  BudgetPeriod,
  BudgetUsage,
  BudgetMemberUsage,
  CreateBudgetRequest,
} from '@/types/admin';
import {
  getBudgetSettings,
  updateBudgetSettings,
  getBudgetUsage,
  getBudgetMembers,
} from '@/services/adminApi';

const PERIOD_OPTIONS: { value: BudgetPeriod; label: string }[] = [
  { value: 'hourly', label: 'Hourly' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
];

const TYPE_OPTIONS: { value: BudgetType; label: string }[] = [
  { value: 'user', label: 'User' },
  { value: 'role', label: 'Role' },
  { value: 'global', label: 'Global (default for everyone)' },
];

const ACTION_OPTIONS = [
  { value: 'block', label: 'Block — refuse new turns' },
  { value: 'warn', label: 'Warn — track only, never block' },
  { value: 'notify', label: 'Notify — track only, never block' },
];

const fmtUsd = (n: number | null) => (n == null ? '—' : `$${n.toFixed(2)}`);

// Seed the edit form from an existing budget (name/description are derived, not edited).
const budgetToFormData = (b: Budget): Partial<CreateBudgetRequest> => ({
  budget_type: b.budget_type,
  target_id: b.target_id ?? undefined,
  target_role: b.target_role ?? undefined,
  max_cost_usd: b.max_cost_usd ?? undefined,
  max_total_tokens: b.max_total_tokens ?? undefined,
  period: b.period,
  soft_limit_percent: b.soft_limit_percent,
  hard_limit_action: b.hard_limit_action,
});

const CREATE_DEFAULTS: Partial<CreateBudgetRequest> = {
  budget_type: 'user',
  period: 'monthly',
  soft_limit_percent: 80,
  hard_limit_action: 'block',
};

// Enforcement settings: enable/disable + the per-user default monthly USD cap that
// applies to anyone without a more specific budget row. Reports the enabled state up
// (onEnabledChange) so the page can warn — near the table — when budgets are inert.
function BudgetSettingsPanel({
  onEnabledChange,
}: {
  onEnabledChange: (enabled: boolean | null) => void;
}) {
  const [settings, setSettings] = useState<BudgetSettings | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [defaultCap, setDefaultCap] = useState('0');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState(false);

  useEffect(() => {
    getBudgetSettings()
      .then((s) => {
        setSettings(s);
        setEnabled(s.enabled);
        setDefaultCap(String(s.default_monthly_cost_limit_usd));
        onEnabledChange(s.enabled);
      })
      .catch((e) => setError((e as Error).message));
  }, [onEnabledChange]);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSavedAt(false);
    try {
      const updated = await updateBudgetSettings({
        enabled,
        default_monthly_cost_limit_usd: parseFloat(defaultCap) || 0,
      });
      setSettings(updated);
      setEnabled(updated.enabled);
      setDefaultCap(String(updated.default_monthly_cost_limit_usd));
      setSavedAt(true);
      onEnabledChange(updated.enabled);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const dirty =
    settings != null &&
    (enabled !== settings.enabled ||
      (parseFloat(defaultCap) || 0) !== settings.default_monthly_cost_limit_usd);

  return (
    <Card variant="bordered">
      <CardContent className="space-y-4">
        <div>
          <h2 className="text-lg font-semibold">Enforcement</h2>
          <p className="text-sm text-gray-400 mt-1">
            When enforcement is on, a user who has reached their spending limit for the period is
            blocked from starting a new turn. While off, usage is still tracked for reporting but
            nothing is blocked.
          </p>
        </div>

        {error && (
          <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-2 rounded text-sm">
            {error}
          </div>
        )}

        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-4 h-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
          />
          <span className="text-sm">Enforce budgets (block over-limit turns)</span>
        </label>

        <Input
          label="Default monthly cap per user (USD)"
          type="number"
          min={0}
          step="0.01"
          value={defaultCap}
          onChange={(e) => setDefaultCap(e.target.value)}
          helperText="Applies to every user without a specific budget below. 0 = no default cap."
        />

        <div className="flex items-center gap-3">
          <Button onClick={handleSave} disabled={!dirty || saving}>
            {saving ? 'Saving…' : 'Save'}
          </Button>
          {savedAt && !dirty && <span className="text-sm text-green-400">Saved</span>}
        </div>
      </CardContent>
    </Card>
  );
}

export function BudgetsPage() {
  const { budgets, isLoading, error, fetchBudgets, createBudget, updateBudget, deleteBudget, clearError } =
    useBudgetStore();
  const { users, fetchUsers } = useUserStore();

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editing, setEditing] = useState<Budget | null>(null);
  const [formData, setFormData] = useState<Partial<CreateBudgetRequest>>({});
  const [formError, setFormError] = useState<string | null>(null);
  // Live current-period use, keyed by budget id. Best-effort: a usage-fetch failure
  // keeps whatever's already shown rather than breaking the budgets list.
  const [usage, setUsage] = useState<Record<string, BudgetUsage>>({});
  // null until the enforcement setting loads; false → budgets below are inert.
  const [enforcementOn, setEnforcementOn] = useState<boolean | null>(null);
  // Role/global drill-down: which budget is expanded + its per-member usage.
  const [expandedBudget, setExpandedBudget] = useState<string | null>(null);
  const [members, setMembers] = useState<BudgetMemberUsage[] | null>(null);
  const [membersLoading, setMembersLoading] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  const loadUsage = useCallback(() => {
    getBudgetUsage()
      .then((rows) => setUsage(Object.fromEntries(rows.map((r) => [r.budget_id, r]))))
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchBudgets();
    fetchUsers();
    loadUsage();
  }, [fetchBudgets, fetchUsers, loadUsage]);

  const openModal = (budget?: Budget) => {
    setEditing(budget ?? null);
    setFormData(budget ? budgetToFormData(budget) : { ...CREATE_DEFAULTS });
    setFormError(null);
    setIsModalOpen(true);
  };

  const openCreateForUser = (userId: string) => {
    setEditing(null);
    setFormData({ ...CREATE_DEFAULTS, budget_type: 'user', target_id: userId });
    setFormError(null);
    setIsModalOpen(true);
  };

  // Deep-link from Usage Reports. Once budgets have loaded, open the user's existing
  // budget for editing (one per user) or a prefilled Create; then clear the nav state
  // so a refresh/back doesn't reopen it. Waits for the list so it can pick edit vs create.
  useEffect(() => {
    const st = location.state as { createForUserId?: string } | null;
    if (!st?.createForUserId || isLoading) return;
    const existing = budgets.find(
      (b) => b.budget_type === 'user' && b.target_id === st.createForUserId && b.is_active
    );
    if (existing) openModal(existing);
    else openCreateForUser(st.createForUserId);
    navigate(location.pathname, { replace: true, state: null });
  }, [location, navigate, isLoading, budgets]);

  const closeModal = () => {
    setIsModalOpen(false);
    setEditing(null);
    setFormError(null);
    clearError();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // A budget with no cap is a no-op that, for a user/role scope, silently exempts
    // the target from the default cap — almost always a mistake. Require one cap.
    if (formData.max_cost_usd == null && formData.max_total_tokens == null) {
      setFormError('Set at least one cap — a cost cap (USD) or a token cap.');
      return;
    }
    setFormError(null);
    try {
      if (editing) {
        // Scope/target/name are fixed after creation — send only the editable fields.
        await updateBudget(editing.id, {
          max_cost_usd: formData.max_cost_usd ?? null,
          max_total_tokens: formData.max_total_tokens ?? null,
          period: formData.period,
          soft_limit_percent: formData.soft_limit_percent,
          hard_limit_action: formData.hard_limit_action,
        });
      } else {
        const payload: CreateBudgetRequest = {
          // name/description omitted — the server derives the name from the scope.
          budget_type: formData.budget_type as BudgetType,
          // Only the matching target is sent for the chosen scope.
          target_id: formData.budget_type === 'user' ? formData.target_id : undefined,
          target_role: formData.budget_type === 'role' ? formData.target_role : undefined,
          max_cost_usd: formData.max_cost_usd ?? undefined,
          max_total_tokens: formData.max_total_tokens ?? undefined,
          period: formData.period,
          soft_limit_percent: formData.soft_limit_percent,
          hard_limit_action: formData.hard_limit_action,
        };
        await createBudget(payload);
        // Refresh use so the new row shows the target's real spend (per-user) now,
        // not a dash until the next page load.
        loadUsage();
      }
      closeModal();
    } catch (e) {
      // Surface server errors (e.g. 409 duplicate, validation) in the modal.
      setFormError((e as Error).message || 'Failed to save budget.');
    }
  };

  const handleDelete = async (id: string) => {
    if (confirm('Delete this budget?')) {
      await deleteBudget(id);
    }
  };

  // Pause/resume without deleting the row (inactive budgets are ignored by enforcement).
  const handleToggleActive = async (b: Budget) => {
    await updateBudget(b.id, { is_active: !b.is_active });
  };

  // Expand a role/global budget to list its members and each one's current-period use.
  const toggleExpand = async (b: Budget) => {
    if (expandedBudget === b.id) {
      setExpandedBudget(null);
      setMembers(null);
      return;
    }
    setExpandedBudget(b.id);
    setMembers(null);
    setMembersLoading(true);
    try {
      setMembers(await getBudgetMembers(b.id));
    } catch {
      setMembers([]);
    } finally {
      setMembersLoading(false);
    }
  };

  const scopeLabel = (b: Budget): string => {
    if (b.budget_type === 'user') {
      const u = users.find((x) => x.id === b.target_id);
      return `User: ${u ? u.display_name || u.email : b.target_id ?? '?'}`;
    }
    if (b.budget_type === 'role') return `Role: ${b.target_role ?? '?'}`;
    return 'Global (default)';
  };

  // Consumption vs. the cap (cost cap takes precedence, else token cap), colored at
  // the soft/hard thresholds. Shared by the per-user column and the member drill-down.
  const renderUseValue = (currentCost: number, currentTokens: number, b: Budget) => {
    const [used, cap, raw] =
      b.max_cost_usd != null
        ? [fmtUsd(currentCost), b.max_cost_usd, currentCost]
        : b.max_total_tokens != null
          ? [currentTokens.toLocaleString(), b.max_total_tokens, currentTokens]
          : [fmtUsd(currentCost), null, currentCost];
    const pct = cap && cap > 0 ? (raw / cap) * 100 : null;
    const color =
      pct == null
        ? 'text-gray-300'
        : pct >= 100
          ? 'text-red-400'
          : pct >= b.soft_limit_percent
            ? 'text-amber-400'
            : 'text-gray-300';
    return (
      <span className={color}>
        {used}
        {pct != null && <span className="text-gray-500"> ({pct.toFixed(0)}%)</span>}
      </span>
    );
  };

  // Current-period consumption vs. the cap. Only user budgets have a single subject;
  // role/global are per-member quotas — expand the row to see each member.
  const renderUse = (b: Budget) => {
    const u = usage[b.id];
    if (b.budget_type !== 'user' || !u || u.current_cost_usd == null) {
      return (
        <span className="text-gray-500" title="Per-member quota — expand to see each member">
          —
        </span>
      );
    }
    return renderUseValue(u.current_cost_usd, u.current_total_tokens ?? 0, b);
  };

  const isExpandable = (b: Budget) => b.budget_type === 'role' || b.budget_type === 'global';

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Budgets</h1>
          <p className="text-gray-400 mt-1">Cap per-user spending (USD) per period</p>
        </div>
        <Button onClick={() => openModal()}>
          <Plus className="w-4 h-4 mr-2" />
          Add Budget
        </Button>
      </div>

      <BudgetSettingsPanel onEnabledChange={setEnforcementOn} />

      {enforcementOn === false && (
        <div className="bg-amber-900/40 border border-amber-700 text-amber-300 px-4 py-3 rounded text-sm">
          Enforcement is off — the budgets below are tracked for reporting but{' '}
          <strong>not enforced</strong>. Turn on “Enforce budgets” above to start blocking
          over-limit turns.
        </div>
      )}

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded">
          {error}
        </div>
      )}

      <Card variant="bordered">
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8"></TableHead>
                  <TableHead>Scope</TableHead>
                  <TableHead>Cost cap</TableHead>
                  <TableHead>Token cap</TableHead>
                  <TableHead>Current use</TableHead>
                  <TableHead>Period</TableHead>
                  <TableHead>At limit</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {budgets.map((b) => (
                  <Fragment key={b.id}>
                  <TableRow>
                    <TableCell className="w-8 pr-0">
                      {isExpandable(b) && (
                        <button
                          onClick={() => toggleExpand(b)}
                          title="Show members and their usage"
                          aria-label="Show members"
                          className="text-gray-400 hover:text-gray-200"
                        >
                          {expandedBudget === b.id ? (
                            <ChevronDown className="w-4 h-4" />
                          ) : (
                            <ChevronRight className="w-4 h-4" />
                          )}
                        </button>
                      )}
                    </TableCell>
                    <TableCell className="font-medium">{scopeLabel(b)}</TableCell>
                    <TableCell>{fmtUsd(b.max_cost_usd)}</TableCell>
                    <TableCell>
                      {b.max_total_tokens ? b.max_total_tokens.toLocaleString() : '—'}
                    </TableCell>
                    <TableCell>{renderUse(b)}</TableCell>
                    <TableCell className="capitalize">{b.period}</TableCell>
                    <TableCell className="capitalize">{b.hard_limit_action}</TableCell>
                    <TableCell>
                      <button
                        onClick={() => handleToggleActive(b)}
                        title={b.is_active ? 'Click to pause' : 'Click to resume'}
                        className={`text-xs px-2 py-1 rounded cursor-pointer ${
                          b.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-gray-700 text-gray-400'
                        }`}
                      >
                        {b.is_active ? 'Active' : 'Inactive'}
                      </button>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button variant="ghost" size="sm" onClick={() => openModal(b)}>
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => handleDelete(b.id)}>
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                  {expandedBudget === b.id && (
                    <TableRow>
                      <TableCell colSpan={9} className="bg-gray-900/50 p-0">
                        {membersLoading ? (
                          <div className="flex items-center justify-center py-4">
                            <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-blue-500" />
                          </div>
                        ) : members && members.length > 0 ? (
                          <table className="w-full text-sm">
                            <thead>
                              <tr className="text-gray-500 text-xs">
                                <th className="text-left pl-12 py-2">Member</th>
                                <th className="text-right py-2 pr-4">Current use (per-member cap)</th>
                              </tr>
                            </thead>
                            <tbody>
                              {members.map((m) => (
                                <tr key={m.user_id} className="border-t border-gray-800">
                                  <td className="pl-12 py-2">
                                    <span className="text-gray-200">{m.display_name}</span>{' '}
                                    <span className="text-gray-500">{m.email}</span>
                                  </td>
                                  <td className="text-right py-2 pr-4">
                                    {renderUseValue(m.current_cost_usd, m.current_total_tokens, b)}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        ) : (
                          <p className="text-center text-gray-500 py-4">
                            No members {b.budget_type === 'role' ? `with role "${b.target_role}"` : ''}
                          </p>
                        )}
                      </TableCell>
                    </TableRow>
                  )}
                  </Fragment>
                ))}
                {budgets.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={9} className="text-center py-8 text-gray-400">
                      No budgets configured. The default monthly cap above applies to all users.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Modal
        isOpen={isModalOpen}
        onClose={closeModal}
        title={editing ? 'Edit Budget' : 'Create Budget'}
        size="lg"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          {formError && (
            <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-2 rounded text-sm">
              {formError}
            </div>
          )}

          {editing ? (
            // Scope is fixed on edit; show what's being edited (the name is derived).
            <div className="text-sm">
              <span className="text-gray-400">Editing budget for </span>
              <span className="font-medium">{scopeLabel(editing)}</span>
            </div>
          ) : (
            <Select
              label="Scope"
              options={TYPE_OPTIONS}
              value={formData.budget_type || 'user'}
              onChange={(e) =>
                setFormData({ ...formData, budget_type: e.target.value as BudgetType })
              }
            />
          )}

          {formData.budget_type === 'user' && !editing && (
            <Select
              label="User"
              placeholder="Select a user"
              options={users.map((u) => ({ value: u.id, label: u.display_name || u.email }))}
              value={formData.target_id || ''}
              onChange={(e) => setFormData({ ...formData, target_id: e.target.value })}
              required
            />
          )}

          {formData.budget_type === 'role' && !editing && (
            <Input
              label="Role name"
              value={formData.target_role || ''}
              onChange={(e) => setFormData({ ...formData, target_role: e.target.value })}
              required
              helperText="Must match a role name from your OIDC provider"
            />
          )}

          <Input
            label="Cost cap (USD)"
            type="number"
            min={0}
            step="0.01"
            value={formData.max_cost_usd ?? ''}
            onChange={(e) =>
              setFormData({
                ...formData,
                max_cost_usd: e.target.value ? parseFloat(e.target.value) : undefined,
              })
            }
            helperText="Max spend per period. Leave empty for no cost cap."
          />

          <Input
            label="Token cap (optional)"
            type="number"
            min={0}
            value={formData.max_total_tokens ?? ''}
            onChange={(e) =>
              setFormData({
                ...formData,
                max_total_tokens: e.target.value ? parseInt(e.target.value) : undefined,
              })
            }
            helperText="Max total tokens per period. Leave empty for no token cap."
          />

          <Select
            label="Period"
            options={PERIOD_OPTIONS}
            value={formData.period || 'monthly'}
            onChange={(e) => setFormData({ ...formData, period: e.target.value as BudgetPeriod })}
          />

          <Input
            label="Warn at (% of limit)"
            type="number"
            min={0}
            max={100}
            value={formData.soft_limit_percent ?? 80}
            onChange={(e) =>
              setFormData({
                ...formData,
                soft_limit_percent: e.target.value ? parseInt(e.target.value) : undefined,
              })
            }
          />

          <Select
            label="At limit"
            options={ACTION_OPTIONS}
            value={formData.hard_limit_action || 'block'}
            onChange={(e) => setFormData({ ...formData, hard_limit_action: e.target.value })}
          />

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={closeModal}>
              Cancel
            </Button>
            <Button type="submit">{editing ? 'Update' : 'Create'}</Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
