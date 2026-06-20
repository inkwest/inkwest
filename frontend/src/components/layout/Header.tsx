import { useState, useRef, useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft, Check, Menu, User, LogOut, Shield } from 'lucide-react';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore, TaskListPosition } from '@/store/settings';
import { useAuthStore } from '@/store/admin';
import { useConfigStore } from '@/store/config';
import { useUpdateStore } from '@/store/update';
import { ConnectionStatus } from '@/components/chat/ConnectionStatus';
import { formatTokenCount } from '@/lib/utils';

const TASK_LIST_OPTIONS: { value: TaskListPosition; label: string }[] = [
  { value: 'hidden', label: 'Hidden' },
  { value: 'top', label: 'Top right' },
  { value: 'bottom', label: 'Bottom right' },
];

export function Header() {
  const navigate = useNavigate();
  // Inside the admin panel the entry flips to "Back to Chat" — otherwise it
  // would only navigate to where the user already is, with no way back.
  const inAdminPanel = useLocation().pathname.startsWith('/admin');
  // Session context belongs to the chat — hide it on admin pages, but keep the
  // store intact so the chat restores as-is when navigating back.
  const storeSession = useSessionsStore((s) => s.currentSession);
  const currentSession = inAdminPanel ? null : storeSession;
  const { compactToolCards, toggleCompactToolCards, showTimestamps, toggleShowTimestamps, taskListPosition, setTaskListPosition, toggleSidebar } = useSettingsStore();
  const { user, oidcEnabled, isAuthenticated, logout } = useAuthStore();
  // fetchConfig still primes the shared config (model picker, attachments gate) once
  // the user is authenticated, even though the header no longer renders a model badge.
  const { fetchConfig } = useConfigStore();
  const version = useUpdateStore((s) => s.loadedVersion);
  const [showMenu, setShowMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // The /api/config endpoint requires auth — fetch only once signed in.
  useEffect(() => {
    if (isAuthenticated) fetchConfig();
  }, [isAuthenticated, fetchConfig]);

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
        <div className="flex flex-col min-w-0 md:hidden">
          <span className="flex items-baseline gap-1.5 min-w-0">
            <span className="text-sm font-semibold leading-tight">Inkwest</span>
            {version && (
              <span className="font-mono text-[9px] text-gray-500 truncate" title={`Version ${version}`}>
                {version}
              </span>
            )}
          </span>
          <span className="text-[10px] text-gray-500 truncate">Secure infrastructure &amp; code assistant</span>
        </div>
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
        {/* The per-conversation model/engine selector lives in the chat toolbar
            (ConnectorPanel). No global default badge here: outside a session it
            named only a model with no engine, which is ambiguous now that a model
            can run on more than one engine. */}
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
                <button
                  onClick={toggleShowTimestamps}
                  className="w-full flex items-center justify-between px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
                >
                  <span>Show timestamps</span>
                  {showTimestamps && <Check className="h-4 w-4 text-green-400" />}
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
                    // A real link (not a button+navigate) so middle-click / ⌘-click opens
                    // the admin panel in a new tab; the onClick just closes the menu on a
                    // plain left-click (it doesn't fire for modified/middle clicks).
                    <Link
                      to={inAdminPanel ? '/' : '/admin'}
                      onClick={() => setShowMenu(false)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
                    >
                      {inAdminPanel ? <ArrowLeft className="h-4 w-4" /> : <Shield className="h-4 w-4" />}
                      {inAdminPanel ? 'Back to Chat' : 'Admin Panel'}
                    </Link>
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
