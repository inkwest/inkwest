import { NavLink, Outlet, Navigate } from 'react-router-dom';
import { clsx } from 'clsx';
import {
  LayoutDashboard,
  FileText,
  Terminal,
  Users,
  BarChart3,
  Cpu,
  Plug,
  Info,
  ArrowLeft,
} from 'lucide-react';
import { useAuthStore } from '@/store/admin';
import { ShellLayout } from '@/components/layout/ShellLayout';

// Grouped by purpose. NOTE: Clusters and Role Mappings are intentionally NOT listed —
// they are slated for decommission; their routes still exist (reachable by URL) and are
// removed in a follow-up MR. Don't re-add them to the nav.
const navGroups: { label?: string; items: { to: string; icon: typeof Cpu; label: string; end?: boolean }[] }[] = [
  { items: [{ to: '/admin', icon: LayoutDashboard, label: 'Dashboard', end: true }] },
  {
    label: 'Agent config',
    items: [
      { to: '/admin/model', icon: Cpu, label: 'Model' },
      { to: '/admin/system-prompts', icon: Terminal, label: 'System Prompts' },
      { to: '/admin/skills', icon: FileText, label: 'Skills' },
      { to: '/admin/mcp', icon: Plug, label: 'MCP Servers' },
    ],
  },
  { label: 'Access', items: [{ to: '/admin/users', icon: Users, label: 'Users' }] },
  {
    label: 'Observability',
    items: [{ to: '/admin/usage', icon: BarChart3, label: 'Usage Reports' }],
  },
  {
    label: 'Diagnostics',
    items: [{ to: '/admin/system-info', icon: Info, label: 'System Info' }],
  },
];

function AdminSidebar() {
  const { user } = useAuthStore();

  return (
    <div className="w-full bg-gray-800 border-r border-gray-700 flex flex-col h-full">
      <div className="p-4 border-b border-gray-700">
        <h1 className="text-xl font-bold">Admin Panel</h1>
        <p className="text-sm text-gray-400 mt-1">{user?.email}</p>
      </div>

      <nav className="flex-1 p-4 space-y-1 overflow-y-auto">
        {navGroups.map((group, i) => (
          <div key={group.label ?? i} className={i > 0 ? 'pt-3' : undefined}>
            {group.label && (
              <p className="px-3 pb-1 text-xs font-semibold uppercase tracking-wider text-gray-500">
                {group.label}
              </p>
            )}
            <div className="space-y-1">
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    clsx(
                      'flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium transition-colors',
                      isActive
                        ? 'bg-blue-600 text-white'
                        : 'text-gray-300 hover:bg-gray-700 hover:text-white'
                    )
                  }
                >
                  <item.icon className="w-5 h-5" />
                  {item.label}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="p-4 border-t border-gray-700">
        <NavLink
          to="/"
          className="flex items-center gap-3 px-3 py-2 text-sm text-gray-400 hover:text-white transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
          Back to Chat
        </NavLink>
      </div>
    </div>
  );
}

export function AdminLayout() {
  const { user, isAuthenticated } = useAuthStore();

  if (!isAuthenticated || !user?.is_admin) {
    return <Navigate to="/" replace />;
  }

  return (
    <ShellLayout sidebar={<AdminSidebar />} mainClassName="flex-1 overflow-y-auto p-6">
      <Outlet />
    </ShellLayout>
  );
}
