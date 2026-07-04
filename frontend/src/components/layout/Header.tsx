import { useState, useRef, useEffect } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft, Menu, User, LogOut, Shield, HelpCircle, BarChart3, Settings } from 'lucide-react';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore } from '@/store/settings';
import { usePreferencesStore } from '@/store/preferences';
import { useAuthStore } from '@/store/admin';
import { useOnboardingStore } from '@/store/onboarding';
import { useConfigStore } from '@/store/config';
import { useUpdateStore } from '@/store/update';
import { ConnectionStatus } from '@/components/chat/ConnectionStatus';
import { SettingsModal } from '@/components/settings/SettingsModal';
import { useSettingsUi, isSettingsTab } from '@/store/settingsUi';
import { devSwitchUser } from '@/services/api';
import { TokenGlyphs } from '@/components/usage/TokenGlyphs';

export function Header() {
  const navigate = useNavigate();
  const location = useLocation();
  // Inside the admin panel the entry flips to "Back to Chat" — otherwise it
  // would only navigate to where the user already is, with no way back.
  const inAdminPanel = location.pathname.startsWith('/admin');
  // Session context belongs to the chat — hide it on admin pages, but keep the
  // store intact so the chat restores as-is when navigating back.
  const storeSession = useSessionsStore((s) => s.currentSession);
  const currentSession = inAdminPanel ? null : storeSession;
  const toggleSidebar = useSettingsStore((s) => s.toggleSidebar);
  const loadPreferences = usePreferencesStore((s) => s.load);
  const { user, oidcEnabled, isAuthenticated, logout } = useAuthStore();
  // fetchConfig still primes the shared config (model picker, attachments gate) once
  // the user is authenticated, even though the header no longer renders a model badge.
  const { fetchConfig } = useConfigStore();
  const version = useUpdateStore((s) => s.loadedVersion);
  const [showMenu, setShowMenu] = useState(false);
  // Settings modal open-state lives in a store so deep components (e.g. the MCP panel) can open
  // it on a specific tab without prop-drilling.
  const settingsOpen = useSettingsUi((s) => s.isOpen);
  const settingsInitialTab = useSettingsUi((s) => s.initialTab);
  const openSettings = useSettingsUi((s) => s.open);
  const closeSettings = useSettingsUi((s) => s.close);
  const [devName, setDevName] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);

  // Dev-only convenience: impersonate a local user (no IdP) to test multi-user
  // sharing. A full reload re-authenticates REST + every WebSocket as the new user.
  const handleDevSwitch = async (name: string) => {
    await devSwitchUser(name.trim());
    window.location.href = '/';
  };

  // The /api/config endpoint requires auth — fetch only once signed in. Also hydrate the
  // DB-backed preferences so UI settings sync across devices on login.
  useEffect(() => {
    if (isAuthenticated) {
      fetchConfig();
      void loadPreferences();
    }
  }, [isAuthenticated, fetchConfig, loadPreferences]);

  // Settings has no page of its own (it's a modal), so ?settings=<tab> is its
  // linkable address: open the modal when the URL carries it (a middle-clicked
  // menu entry landing in a fresh tab), then strip the param so reload/back
  // don't re-open it. Runs in Header because it renders the modal on every layout.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const tab = params.get('settings');
    if (tab === null) return;
    openSettings(isSettingsTab(tab) ? tab : undefined);
    params.delete('settings');
    navigate({ pathname: location.pathname, search: params.toString() }, { replace: true });
  }, [location.search, location.pathname, navigate, openSettings]);

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
        {currentSession && (currentSession.total_input_tokens || currentSession.total_output_tokens || currentSession.total_cache_read_tokens || currentSession.total_cache_write_tokens) ? (
          <TokenGlyphs
            className="text-xs text-gray-500 hidden sm:inline"
            input={currentSession.total_input_tokens || 0}
            output={currentSession.total_output_tokens || 0}
            cacheRead={currentSession.total_cache_read_tokens || 0}
            cacheWrite={currentSession.total_cache_write_tokens || 0}
          />
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

              {/* Dev-only (no OIDC): switch the impersonated local user for testing. */}
              {!oidcEnabled && (
                <div className="px-3 py-2 border-b border-gray-700 space-y-1.5">
                  <span className="text-xs text-gray-500 uppercase tracking-wider">
                    Dev: switch user
                  </span>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      void handleDevSwitch(devName);
                    }}
                    className="flex gap-1"
                  >
                    <input
                      value={devName}
                      onChange={(e) => setDevName(e.target.value)}
                      placeholder="username (e.g. bob)"
                      className="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 placeholder-gray-500 focus:outline-none focus:border-blue-500"
                    />
                    <button
                      type="submit"
                      className="px-2 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white"
                    >
                      Switch
                    </button>
                  </form>
                  {/* Only offer the reset when actually impersonating someone else. */}
                  {user && user.email !== 'dev@localhost' && (
                    <button
                      onClick={() => void handleDevSwitch('')}
                      className="text-xs text-gray-400 hover:text-gray-200"
                    >
                      Reset to dev@localhost
                    </button>
                  )}
                </div>
              )}

              <div className="p-2 space-y-1 border-b border-gray-700">
                {/* Self-service usage report — available to every user, not just admins. */}
                <Link
                  to="/usage"
                  onClick={() => setShowMenu(false)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
                >
                  <BarChart3 className="h-4 w-4" />
                  My Usage
                </Link>
              </div>

              <div className="p-2 space-y-1">
                {/* Real link so middle-click / ⌘-click opens Settings in a new tab
                    (via the ?settings= effect above). A plain left-click is
                    intercepted to open the modal in place without URL churn. */}
                <Link
                  to={{ search: '?settings=display' }}
                  onClick={(e) => {
                    setShowMenu(false);
                    const plainLeftClick =
                      e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey;
                    if (plainLeftClick) {
                      e.preventDefault();
                      openSettings();
                    }
                  }}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
                >
                  <Settings className="h-4 w-4" />
                  <span>Settings</span>
                </Link>
                {/* Replay the first-run tour. Available to every user (admins included);
                    it runs on the start screen, so jump there and clear the seen flag. */}
                {user && (
                  <button
                    onClick={() => {
                      setShowMenu(false);
                      useOnboardingStore.getState().restart();
                      navigate('/');
                    }}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 rounded-md"
                  >
                    <HelpCircle className="h-4 w-4" />
                    <span>Replay tutorial</span>
                  </button>
                )}
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

      <SettingsModal
        isOpen={settingsOpen}
        initialTab={settingsInitialTab}
        onClose={closeSettings}
      />
    </header>
  );
}
