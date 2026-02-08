import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Check, Menu, User, LogOut, Shield } from 'lucide-react';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore, TaskListPosition } from '@/store/settings';
import { useAuthStore } from '@/store/admin';
import { ConnectionStatus } from '@/components/chat/ConnectionStatus';
import { formatTokenCount } from '@/lib/utils';

const TASK_LIST_OPTIONS: { value: TaskListPosition; label: string }[] = [
  { value: 'hidden', label: 'Hidden' },
  { value: 'top', label: 'Top right' },
  { value: 'bottom', label: 'Bottom right' },
];

export function Header() {
  const navigate = useNavigate();
  const currentSession = useSessionsStore((s) => s.currentSession);
  const { compactToolCards, toggleCompactToolCards, taskListPosition, setTaskListPosition, toggleSidebar } = useSettingsStore();
  const { user, oidcEnabled, logout } = useAuthStore();
  const [showMenu, setShowMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setShowMenu(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleLogout = async () => {
    await logout();
    navigate('/');
  };

  return (
    <header className="h-14 border-b border-gray-700 px-4 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <button
          onClick={toggleSidebar}
          className="p-2 hover:bg-gray-700 rounded-md transition-colors md:hidden"
        >
          <Menu className="h-5 w-5 text-gray-400" />
        </button>
        <span className="text-sm font-semibold md:hidden">Inkwest</span>
        {currentSession && (
          <div className="flex flex-col min-w-0">
            {currentSession.title && (
              <span className="text-sm text-gray-300 truncate">{currentSession.title}</span>
            )}
            {currentSession.cluster && (
              <span className="text-xs text-gray-500 truncate">{currentSession.cluster.display_name}</span>
            )}
          </div>
        )}
      </div>
      <div className="flex items-center gap-3">
        {currentSession && (currentSession.total_input_tokens || currentSession.total_output_tokens) ? (
          <span className="text-xs text-gray-500 hidden sm:inline" title="Input / Output tokens">
            {formatTokenCount(currentSession.total_input_tokens || 0)}&darr; {formatTokenCount(currentSession.total_output_tokens || 0)}&uarr;
          </span>
        ) : null}
        {currentSession && <ConnectionStatus />}

        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setShowMenu(!showMenu)}
            className="p-2 hover:bg-gray-700 rounded-md transition-colors"
            title="Profile & Settings"
          >
            <User className="h-4 w-4 text-gray-400" />
          </button>

          {showMenu && (
            <div className="absolute right-0 top-full mt-1 w-56 bg-gray-800 border border-gray-700 rounded-md shadow-lg z-50">
              {user && (
                <div className="px-3 py-2 border-b border-gray-700">
                  <div className="text-sm text-gray-200 truncate">{user.display_name}</div>
                  <div className="text-xs text-gray-500 truncate">{user.email}</div>
                </div>
              )}

              <div className="p-2 space-y-1">
                <div className="px-3 py-1">
                  <span className="text-xs text-gray-500 uppercase tracking-wider">Settings</span>
                </div>
                <button
                  onClick={toggleCompactToolCards}
                  className="w-full flex items-center justify-between px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
                >
                  <span>Compact tool cards</span>
                  {compactToolCards && <Check className="h-4 w-4 text-green-400" />}
                </button>

                <div className="px-3 py-2">
                  <span className="text-xs text-gray-500 uppercase tracking-wider">Task list</span>
                  <div className="mt-1 flex gap-1">
                    {TASK_LIST_OPTIONS.map((opt) => (
                      <button
                        key={opt.value}
                        onClick={() => setTaskListPosition(opt.value)}
                        className={`flex-1 px-2 py-1 text-xs rounded ${
                          taskListPosition === opt.value
                            ? 'bg-blue-600 text-white'
                            : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {(user?.is_admin || oidcEnabled) && (
                <div className="border-t border-gray-700 p-2 space-y-1">
                  {user?.is_admin && (
                    <button
                      onClick={() => { navigate('/admin'); setShowMenu(false); }}
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
                    >
                      <Shield className="h-4 w-4" />
                      Admin Panel
                    </button>
                  )}
                  {oidcEnabled && (
                    <button
                      onClick={handleLogout}
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm text-red-400 hover:bg-gray-700 rounded-md"
                    >
                      <LogOut className="h-4 w-4" />
                      Logout
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
