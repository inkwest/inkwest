import { useEffect, useState } from 'react';
import { Plus, Edit2, Trash2 } from 'lucide-react';
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
  Badge,
  Modal,
  Input,
  Select,
} from '@/components/ui';
import { useBudgetStore } from '@/store/admin';
import { Budget, CreateBudgetRequest, BudgetType, BudgetPeriod } from '@/types/admin';

const budgetTypeOptions = [
  { value: 'global', label: 'Global' },
  { value: 'user', label: 'User' },
  { value: 'role', label: 'Role' },
];

const periodOptions = [
  { value: 'hourly', label: 'Hourly' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
];

export function BudgetsPage() {
  const {
    budgets,
    isLoading,
    error,
    fetchBudgets,
    createBudget,
    updateBudget,
    deleteBudget,
    clearError,
  } = useBudgetStore();

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingBudget, setEditingBudget] = useState<Budget | null>(null);
  const [formData, setFormData] = useState<Partial<CreateBudgetRequest>>({
    name: '',
    budget_type: 'global',
    period: 'daily',
    soft_limit_percent: 80,
  });

  useEffect(() => {
    fetchBudgets();
  }, [fetchBudgets]);

  const handleOpenModal = (budget?: Budget) => {
    if (budget) {
      setEditingBudget(budget);
      setFormData({
        name: budget.name,
        description: budget.description || '',
        budget_type: budget.budget_type,
        max_total_tokens: budget.max_total_tokens || undefined,
        max_cost_usd: budget.max_cost_usd ?? undefined,
        period: budget.period,
        soft_limit_percent: budget.soft_limit_percent,
      });
    } else {
      setEditingBudget(null);
      setFormData({
        name: '',
        budget_type: 'global',
        period: 'daily',
        soft_limit_percent: 80,
      });
    }
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditingBudget(null);
    clearError();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (editingBudget) {
        await updateBudget(editingBudget.id, formData);
      } else {
        await createBudget(formData as CreateBudgetRequest);
      }
      handleCloseModal();
    } catch (err) {
      // Error is handled by the store
    }
  };

  const handleDelete = async (id: string) => {
    if (confirm('Are you sure you want to delete this budget?')) {
      await deleteBudget(id);
    }
  };

  const handleToggleActive = async (budget: Budget) => {
    await updateBudget(budget.id, { is_active: !budget.is_active });
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Budgets</h1>
          <p className="text-gray-400 mt-1">Track token and cost usage</p>
        </div>
        <Button onClick={() => handleOpenModal()}>
          <Plus className="w-4 h-4 mr-2" />
          Add Budget
        </Button>
      </div>

      {/* Budget is informational only, never enforced — see docs/BUDGET.md */}
      <div className="bg-blue-900/30 border border-blue-700 text-blue-300 px-4 py-3 rounded text-sm">
        Budgets are <strong>informational only</strong>. They track usage for
        reporting and visibility — no limit blocks, throttles, or interrupts a
        conversation.
      </div>

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
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Period</TableHead>
                  <TableHead>Token Limit</TableHead>
                  <TableHead>Cost Limit</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {budgets.map((budget) => (
                  <TableRow key={budget.id}>
                    <TableCell className="font-medium">{budget.name}</TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          budget.budget_type === 'global'
                            ? 'info'
                            : budget.budget_type === 'user'
                            ? 'success'
                            : 'warning'
                        }
                      >
                        {budget.budget_type}
                      </Badge>
                    </TableCell>
                    <TableCell>{budget.period}</TableCell>
                    <TableCell>
                      {budget.max_total_tokens
                        ? budget.max_total_tokens.toLocaleString()
                        : '-'}
                    </TableCell>
                    <TableCell>
                      {budget.max_cost_usd ? `$${budget.max_cost_usd}` : '-'}
                    </TableCell>
                    <TableCell>
                      <button
                        onClick={() => handleToggleActive(budget)}
                        className={`text-xs px-2 py-1 rounded cursor-pointer ${
                          budget.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-red-900/50 text-red-400'
                        }`}
                      >
                        {budget.is_active ? 'Active' : 'Inactive'}
                      </button>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleOpenModal(budget)}
                        >
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDelete(budget.id)}
                        >
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {budgets.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center py-8 text-gray-400">
                      No budgets configured. Click "Add Budget" to create one.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Budget Form Modal */}
      <Modal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        title={editingBudget ? 'Edit Budget' : 'Create Budget'}
        size="md"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label="Name"
            value={formData.name || ''}
            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
            required
          />

          <Input
            label="Description"
            value={formData.description || ''}
            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
          />

          <Select
            label="Budget Type"
            options={budgetTypeOptions}
            value={formData.budget_type || 'global'}
            onChange={(e) =>
              setFormData({ ...formData, budget_type: e.target.value as BudgetType })
            }
          />

          <Select
            label="Period"
            options={periodOptions}
            value={formData.period || 'daily'}
            onChange={(e) =>
              setFormData({ ...formData, period: e.target.value as BudgetPeriod })
            }
          />

          <Input
            label="Max Total Tokens"
            type="number"
            value={formData.max_total_tokens || ''}
            onChange={(e) =>
              setFormData({
                ...formData,
                max_total_tokens: e.target.value ? parseInt(e.target.value) : undefined,
              })
            }
            helperText="Leave empty for no limit"
          />

          <Input
            label="Max Cost (USD)"
            type="number"
            step="0.01"
            value={formData.max_cost_usd || ''}
            onChange={(e) =>
              setFormData({
                ...formData,
                max_cost_usd: e.target.value ? parseFloat(e.target.value) : undefined,
              })
            }
            helperText="Leave empty for no limit"
          />

          <Input
            label="Soft Limit %"
            type="number"
            min="0"
            max="100"
            value={formData.soft_limit_percent || 80}
            onChange={(e) =>
              setFormData({ ...formData, soft_limit_percent: parseInt(e.target.value) })
            }
            helperText="Warning threshold before hard limit"
          />

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={handleCloseModal}>
              Cancel
            </Button>
            <Button type="submit">
              {editingBudget ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
