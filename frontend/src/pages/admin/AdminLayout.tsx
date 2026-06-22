import { NavLink, Outlet, Navigate } from 'react-router-dom';
import { clsx } from 'clsx';
import {
  LayoutDashboard,
  Server,
  FileText,
  Terminal,
  Users,
  Shield,
  BarChart3,
  Cpu,
  Info,
  ArrowLeft,
} from 'lucide-react';
import { useAuthStore } from '@/store/admin';
import { ShellLayout } from '@/components/layout/ShellLayout';

const navItems = [
  { to: '/admin', icon: LayoutDashboard, label: 'Dashboard', end: true },
  { to: '/admin/clusters', icon: Server, label: 'Clusters' },
  { to: '/admin/skills', icon: FileText, label: 'Skills' },
  { to: '/admin/system-prompts', icon: Terminal, label: 'System Prompts' },
  { to: '/admin/users', icon: Users, label: 'Users' },
  { to: '/admin/roles', icon: Shield, label: 'Role Mappings' },
  { to: '/admin/usage', icon: BarChart3, label: 'Usage Reports' },
  { to: '/admin/model', icon: Cpu, label: 'Model' },
  { to: '/admin/system-info', icon: Info, label: 'System Info' },
];

function AdminSidebar() {
  const { user } = useAuthStore();

  return (
    <div className="w-full bg-gray-800 border-r border-gray-700 flex flex-col h-full">
      <div className="p-4 border-b border-gray-700">
        <h1 className="text-xl font-bold">Admin Panel</h1>
        <p className="text-sm text-gray-400 mt-1">{user?.email}</p>
      </div>

      <nav className="flex-1 p-4 space-y-1">
        {navItems.map((item) => (
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
