import { useEffect } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui';
import { useUsageStore, useBudgetStore, useUserStore } from '@/store/admin';
import { Users, DollarSign, Cpu, TrendingUp } from 'lucide-react';

export function DashboardPage() {
  const { summary, fetchAll: fetchUsage, isLoading: usageLoading } = useUsageStore();
  const { budgets, fetchBudgets, isLoading: budgetLoading } = useBudgetStore();
  const { users, fetchUsers, isLoading: userLoading } = useUserStore();

  useEffect(() => {
    fetchUsage();
    fetchBudgets();
    fetchUsers();
  }, [fetchUsage, fetchBudgets, fetchUsers]);

  const isLoading = usageLoading || budgetLoading || userLoading;

  const stats = [
    {
      title: 'Active Users',
      value: users.filter((u) => u.is_active).length,
      icon: Users,
      color: 'text-blue-400',
      bgColor: 'bg-blue-900/20',
    },
    {
      title: 'Active Budgets',
      value: budgets.filter((b) => b.is_active).length,
      icon: DollarSign,
      color: 'text-green-400',
      bgColor: 'bg-green-900/20',
    },
    {
      title: 'Today\'s Tokens',
      value: summary ? (summary.total_tokens / 1000).toFixed(1) + 'K' : '-',
      icon: Cpu,
      color: 'text-purple-400',
      bgColor: 'bg-purple-900/20',
    },
    {
      title: 'Today\'s Cost',
      value: summary ? `$${summary.total_cost_usd}` : '-',
      icon: TrendingUp,
      color: 'text-yellow-400',
      bgColor: 'bg-yellow-900/20',
    },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="text-gray-400 mt-1">Overview of system usage and status</p>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
        </div>
      ) : (
        <>
          {/* Stats Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {stats.map((stat) => (
              <Card key={stat.title} variant="bordered">
                <CardContent className="flex items-center gap-4">
                  <div className={`p-3 rounded-lg ${stat.bgColor}`}>
                    <stat.icon className={`w-6 h-6 ${stat.color}`} />
                  </div>
                  <div>
                    <p className="text-sm text-gray-400">{stat.title}</p>
                    <p className="text-2xl font-bold">{stat.value}</p>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>

          {/* Recent Activity */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <Card variant="bordered">
              <CardHeader>
                <CardTitle>Usage Summary</CardTitle>
              </CardHeader>
              <CardContent>
                {summary ? (
                  <div className="space-y-3">
                    <div className="flex justify-between">
                      <span className="text-gray-400">Period</span>
                      <span>{summary.period}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-400">Total Requests</span>
                      <span>{summary.request_count}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-400">Input Tokens</span>
                      <span>{summary.total_input_tokens.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-400">Output Tokens</span>
                      <span>{summary.total_output_tokens.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-400">Total Cost</span>
                      <span>${summary.total_cost_usd.toFixed(4)}</span>
                    </div>
                  </div>
                ) : (
                  <p className="text-gray-400">No usage data available</p>
                )}
              </CardContent>
            </Card>

            <Card variant="bordered">
              <CardHeader>
                <CardTitle>Recent Users</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-3">
                  {users.slice(0, 5).map((user) => (
                    <div
                      key={user.id}
                      className="flex items-center justify-between py-2 border-b border-gray-700 last:border-0"
                    >
                      <div>
                        <p className="font-medium">{user.display_name}</p>
                        <p className="text-sm text-gray-400">{user.email}</p>
                      </div>
                      <span
                        className={`text-xs px-2 py-1 rounded ${
                          user.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-red-900/50 text-red-400'
                        }`}
                      >
                        {user.is_active ? 'Active' : 'Inactive'}
                      </span>
                    </div>
                  ))}
                  {users.length === 0 && (
                    <p className="text-gray-400">No users found</p>
                  )}
                </div>
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
