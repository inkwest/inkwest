import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Cloud, Gitlab } from 'lucide-react';
import { useSessionsStore } from '@/store/sessions';
import { useAuthStore, useSystemPromptStore } from '@/store/admin';
import { useConfigStore } from '@/store/config';
import { ConnectorName } from '@/types/connectors';
import { ChatInput } from '@/components/chat/ChatInput';
import { ConnectorPanel } from '@/components/connector/ConnectorPanel';
import { McpPanel } from '@/components/connector/McpPanel';

const CUSTOM = '__custom__';

// Connectors offered before a session exists. The real per-session ConnectorPanel
// (status-aware, only enabled connectors) takes over the moment the session is created.
const PRESESSION_CONNECTORS: { name: ConnectorName; label: string; Icon: typeof Cloud }[] = [
  { name: 'aws', label: 'AWS', Icon: Cloud },
  { name: 'gitlab', label: 'GitLab', Icon: Gitlab },
];

// Centered "ready to start a conversation" surface shown at the session root.
// The session is created silently ("en soumsoum") on the first real interaction —
// sending a message, or granting external access — never on mere page load.
export function StartConversation() {
  const navigate = useNavigate();
  const { createSession, setPendingPrompt } = useSessionsStore();
  const { user } = useAuthStore();
  const isAdmin = !!user?.is_admin;
  const { prompts, fetchPrompts } = useSystemPromptStore();

  // '' = default prompt, CUSTOM = free-text, otherwise a system prompt id (admin only).
  const [selectedPrompt, setSelectedPrompt] = useState('');
  const [customPrompt, setCustomPrompt] = useState('');

  // Per-session model choice; '' until the config (default + enabled list) loads.
  const { config, fetchConfig } = useConfigStore();
  const [selectedModel, setSelectedModel] = useState('');

  // Lazily-created session: null until the first interaction materializes it.
  const [sessionId, setSessionId] = useState<string | null>(null);
  const creatingRef = useRef<Promise<string> | null>(null);
  const [autoStart, setAutoStart] = useState<ConnectorName | null>(null);
  const [mcpAutoOpen, setMcpAutoOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isAdmin) fetchPrompts(true); // active only
  }, [isAdmin, fetchPrompts]);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  // Preselect the default model once loaded (don't clobber a manual pick).
  useEffect(() => {
    if (config) setSelectedModel((cur) => cur || config.model);
  }, [config]);

  // Preselect the default prompt once loaded (don't clobber a manual pick).
  useEffect(() => {
    if (!isAdmin) return;
    const def = prompts.find((p) => p.is_default);
    if (def) setSelectedPrompt((cur) => (cur === '' ? def.id : cur));
  }, [isAdmin, prompts]);

  const customInvalid = isAdmin && selectedPrompt === CUSTOM && !customPrompt.trim();

  // Create the session once, capturing the admin system-prompt choice made up to this point.
  const ensureSession = useCallback(async (): Promise<string> => {
    if (sessionId) return sessionId;
    if (!creatingRef.current) {
      const opts: { systemPromptId?: string; systemPromptCustom?: string; model?: string } =
        isAdmin && selectedPrompt === CUSTOM
          ? { systemPromptCustom: customPrompt.trim() }
          : isAdmin && selectedPrompt
            ? { systemPromptId: selectedPrompt }
            : {};
      // Pin the model only on an explicit non-default pick — untouched sessions
      // keep following the global default if the admin later changes it.
      if (selectedModel && selectedModel !== config?.model) {
        opts.model = selectedModel;
      }
      creatingRef.current = createSession(undefined, opts)
        .then((s) => {
          setSessionId(s.id);
          return s.id;
        })
        .catch((e) => {
          // A transient failure must not poison the dedupe ref — clear it so the
          // next interaction can retry instead of replaying a rejected promise.
          creatingRef.current = null;
          throw e;
        });
    }
    return creatingRef.current;
  }, [sessionId, isAdmin, selectedPrompt, customPrompt, selectedModel, config, createSession]);

  const handleSend = async (message: string) => {
    if (customInvalid) return;
    setError(null);
    try {
      const id = await ensureSession();
      // The session page seeds + auto-submits this once its WebSocket connects;
      // the text stays recoverable in the composer until it actually sends.
      setPendingPrompt(message);
      navigate(`/session/${id}`);
    } catch (e) {
      setError(`Could not start the session: ${(e as Error).message}`);
    }
  };

  const handleGiveAccess = async (connector: ConnectorName) => {
    if (customInvalid) return;
    setError(null);
    try {
      await ensureSession();
      setAutoStart(connector);
    } catch (e) {
      setError(`Could not start the session: ${(e as Error).message}`);
    }
  };

  // Create the session before the MCP picker opens, so the selection persists.
  // Creating the session swaps this branch to ConnectorPanel (remounting the MCP
  // row), so flag autoOpen to reopen the picker on the post-session instance.
  const handleConfigureMcp = async () => {
    if (customInvalid) return;
    setError(null);
    try {
      setMcpAutoOpen(true);
      await ensureSession();
    } catch (e) {
      setMcpAutoOpen(false);
      setError(`Could not start the session: ${(e as Error).message}`);
    }
  };

  return (
    <div className="flex-1 flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-3xl space-y-4">
        <div className="text-center space-y-1">
          <h1 className="text-2xl font-semibold text-gray-100">Start a conversation</h1>
          <p className="text-sm text-gray-500">
            Ask a question to begin — or grant external access first. Your session starts
            automatically on your first message.
          </p>
        </div>

        {isAdmin && (
          <div>
            <label className="block text-sm text-gray-400 mb-2">System Prompt</label>
            <select
              value={selectedPrompt}
              onChange={(e) => setSelectedPrompt(e.target.value)}
              className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white"
            >
              {prompts.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                  {p.is_default ? ' (default)' : ''}
                </option>
              ))}
              <option value={CUSTOM}>Custom…</option>
            </select>
            {selectedPrompt === CUSTOM && (
              <textarea
                value={customPrompt}
                onChange={(e) => setCustomPrompt(e.target.value)}
                rows={6}
                placeholder="Enter a custom system prompt for this session…"
                className="mt-2 w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white font-mono text-sm"
              />
            )}
          </div>
        )}

        {config && config.available_models.length > 1 && (
          <div>
            <label className="block text-sm text-gray-400 mb-2">Model</label>
            <select
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white"
            >
              {config.available_models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                  {m.id === config.model ? ' (default)' : ''}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* External access + MCP — one row each. Once the session exists, hand off
            to the real status-aware panel (which renders the MCP row itself). */}
        {sessionId ? (
          <div className="rounded-lg border border-gray-700">
            <ConnectorPanel
              sessionId={sessionId}
              autoStart={autoStart}
              layout="stacked"
              autoOpenMcp={mcpAutoOpen}
            />
          </div>
        ) : (
          <div className="flex flex-col gap-1 rounded-lg border border-gray-700 bg-gray-900/60 px-4 py-2">
            <span className="text-xs text-gray-500 mb-0.5">External access:</span>
            {PRESESSION_CONNECTORS.map(({ name, label, Icon }) => (
              <div key={name} className="flex w-full items-center justify-between rounded-md px-2 py-1">
                <div className="flex items-center gap-2">
                  <Icon className="h-4 w-4 text-gray-400" />
                  <span className="text-sm text-gray-200">{label}</span>
                </div>
                <button
                  onClick={() => handleGiveAccess(name)}
                  disabled={customInvalid}
                  className="text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50"
                >
                  Give access
                </button>
              </div>
            ))}
            {/* MCP servers — same bordered box, lazily creates the session on open.
                deferOpen: this instance unmounts when the session is created; the
                post-session ConnectorPanel reopens the picker via autoOpenMcp. */}
            <McpPanel
              sessionId={sessionId ?? ''}
              variant="row"
              onBeforeOpen={handleConfigureMcp}
              deferOpen
            />
          </div>
        )}

        {error && (
          <div role="alert" className="rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">
            {error}
          </div>
        )}

        <ChatInput
          large
          onSend={handleSend}
          disabled={customInvalid}
          placeholder="Describe what you want to investigate or analyse…"
        />
      </div>
    </div>
  );
}
