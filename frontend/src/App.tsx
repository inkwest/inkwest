import { useEffect, useRef } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Layout } from '@/components/layout/Layout';
import { ChatPage } from '@/pages/ChatPage';
import { NewSessionPage } from '@/pages/NewSessionPage';
import { QuickStartPage } from '@/pages/QuickStartPage';
import { AuthCallbackPage } from '@/pages/AuthCallbackPage';
import {
  AdminLayout,
  DashboardPage,
  ClustersPage,
  SkillsPage,
  SystemPromptsPage,
  UsersPage,
  RoleMappingsPage,
  UsagePage,
  ModelSettingsPage,
  McpServersPage,
  SystemInfoPage,
} from '@/pages/admin';
import { useSettingsStore } from '@/store/settings';
import { useAuthStore } from '@/store/admin';
import { getLoginUrl } from '@/services/adminApi';
import { TEXT_SIZE } from '@/lib/utils';

function LoginPrompt() {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-gray-900 text-white gap-6">
      <div className="flex flex-col items-center gap-1">
        <h1 className="text-2xl font-bold">Inkwest</h1>
        <p className="text-sm text-gray-400">Secure infrastructure &amp; code assistant</p>
      </div>
      <p className="text-gray-400">Sign in to continue</p>
      <a
        href={getLoginUrl()}
        className="px-6 py-2 bg-blue-600 hover:bg-blue-500 rounded-md text-sm font-medium transition-colors"
      >
        Sign in with SSO
      </a>
    </div>
  );
}

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, oidcEnabled, loginRedirectMode, isLoading, checkAuth } = useAuthStore();
  const didCheck = useRef(false);
  const redirecting = useRef(false);

  useEffect(() => {
    if (!didCheck.current) {
      didCheck.current = true;
      checkAuth();
    }
  }, [checkAuth]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <p className="text-muted-foreground">Loading…</p>
      </div>
    );
  }

  if (!isAuthenticated && oidcEnabled) {
    if (loginRedirectMode === 'prompt') {
      return <LoginPrompt />;
    }
    if (!redirecting.current) {
      redirecting.current = true;
      window.location.href = getLoginUrl();
    }
    return null;
  }

  redirecting.current = false;

  return <>{children}</>;
}

function App() {
  const compact = useSettingsStore((s) => s.compactToolCards);

  useEffect(() => {
    const cls = TEXT_SIZE.body(compact);
    document.body.classList.add(cls);
    return () => document.body.classList.remove(cls);
  }, [compact]);

  return (
    <BrowserRouter>
      <Routes>
        {/* OIDC callback — outside auth guard */}
        <Route path="/auth/callback" element={<AuthCallbackPage />} />

        {/* Main Chat Routes */}
        <Route path="/" element={<AuthGuard><Layout /></AuthGuard>}>
          <Route index element={<NewSessionPage />} />
          <Route path="session/:sessionId" element={<ChatPage />} />
          <Route path="start" element={<QuickStartPage />} />
        </Route>

        {/* Admin Routes */}
        <Route path="/admin" element={<AuthGuard><AdminLayout /></AuthGuard>}>
          <Route index element={<DashboardPage />} />
          <Route path="clusters" element={<ClustersPage />} />
          <Route path="skills" element={<SkillsPage />} />
          <Route path="system-prompts" element={<SystemPromptsPage />} />
<Route path="users" element={<UsersPage />} />
          <Route path="roles" element={<RoleMappingsPage />} />
          <Route path="usage" element={<UsagePage />} />
          <Route path="model" element={<ModelSettingsPage />} />
          <Route path="mcp" element={<McpServersPage />} />
          <Route path="system-info" element={<SystemInfoPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
