import { useCallback, useEffect, useRef, useState, type ComponentType } from 'react';
import { useNavigate } from 'react-router-dom';
import { Gitlab, X } from 'lucide-react';
import { AwsIcon } from '@/components/icons/BrandIcons';
import { useSessionsStore } from '@/store/sessions';
import { useAuthStore, useSystemPromptStore } from '@/store/admin';
import { useConfigStore } from '@/store/config';
import { ConnectorName } from '@/types/connectors';
import { ChatInput } from '@/components/chat/ChatInput';
import { Attachment } from '@/types/api';
import { ConnectorPanel } from '@/components/connector/ConnectorPanel';
import { ConnectorIconButton } from '@/components/connector/ConnectorIconButton';
import { StateBadge } from '@/components/connector/StateBadge';
import { McpPanel } from '@/components/connector/McpPanel';
import { ModelEnginePicker } from '@/components/chat/ModelEnginePicker';
import { OnboardingTour } from '@/components/onboarding/OnboardingTour';
import { useOnboardingStore } from '@/store/onboarding';
import { useComposerStore } from '@/store/composer';
import { captureLinkPrefill, takeLinkPrefill } from '@/lib/linkPrefill';

const CUSTOM = '__custom__';

// Connectors offered before a session exists. The real per-session ConnectorPanel
// (status-aware, only enabled connectors) takes over the moment the session is created.
const PRESESSION_CONNECTORS: {
  name: ConnectorName;
  label: string;
  Icon: ComponentType<{ className?: string }>;
}[] = [
  { name: 'aws', label: 'AWS', Icon: AwsIcon },
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
  // Mirrors OnboardingTour's own `active` gate: this component co-mounts the tour, so it
  // owns the attach-icon spotlight anchor (tour step 1) — scoped here, not read globally
  // inside ChatInput, so ChatView's composer never renders a phantom disabled icon.
  const tourCompleted = useOnboardingStore((s) => s.completed);
  const tourAnchor = !!user && !tourCompleted;
  const { prompts, fetchPrompts } = useSystemPromptStore();

  // '' = default prompt, CUSTOM = free-text, otherwise a system prompt id (admin only).
  const [selectedPrompt, setSelectedPrompt] = useState('');
  const [customPrompt, setCustomPrompt] = useState('');

  // Per-session Engine·Model choice; null = follow the global default (don't pin).
  const { config, fetchConfig } = useConfigStore();
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [selectedEngine, setSelectedEngine] = useState<string | null>(null);

  // Lazily-created session: null until the first interaction materializes it.
  const [sessionId, setSessionId] = useState<string | null>(null);
  const creatingRef = useRef<Promise<string> | null>(null);
  const [autoStart, setAutoStart] = useState<ConnectorName | null>(null);
  const [mcpAutoOpen, setMcpAutoOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkPrefilled, setLinkPrefilled] = useState(false);

  useEffect(() => {
    if (isAdmin) fetchPrompts(true); // active only
  }, [isAdmin, fetchPrompts]);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  // StrictMode re-runs this: the second take is null and must not clear the notice.
  useEffect(() => {
    const applyPrefill = () => {
      const prefill = takeLinkPrefill();
      if (!prefill) return;
      useComposerStore.getState().inject(prefill);
      setLinkPrefilled(true);
    };
    applyPrefill();
    // A link opened in a tab already on `/` only fires hashchange: there is no boot capture.
    const onHashChange = () => {
      captureLinkPrefill();
      applyPrefill();
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

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
      const opts: {
        systemPromptId?: string;
        systemPromptCustom?: string;
        model?: string;
        engine?: string;
      } =
        isAdmin && selectedPrompt === CUSTOM
          ? { systemPromptCustom: customPrompt.trim() }
          : isAdmin && selectedPrompt
            ? { systemPromptId: selectedPrompt }
            : {};
      // Pin the Engine·Model only on an explicit pick (null = Default) — untouched
      // sessions keep following the global default if the admin later changes it.
      if (selectedModel) {
        opts.model = selectedModel;
        if (selectedEngine) opts.engine = selectedEngine;
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
  }, [sessionId, isAdmin, selectedPrompt, customPrompt, selectedModel, selectedEngine, createSession]);

  const handleSend = async (message: string, attachments?: Attachment[]) => {
    if (customInvalid) return;
    setError(null);
    setLinkPrefilled(false);
    try {
      const id = await ensureSession();
      // The session page seeds + auto-submits this once its WebSocket connects;
      // the text + any attachments stay recoverable until it actually sends. The
      // files were already uploaded to this session, so the descriptors travel along.
      setPendingPrompt(message, attachments ?? null);
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
      {/* First-run guided tour (non-admins only); spotlights the composer affordances. */}
      <OnboardingTour />
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

        {error && (
          <div role="alert" className="rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">
            {error}
          </div>
        )}

        {linkPrefilled && (
          <div
            role="status"
            className="flex items-center justify-between gap-2 rounded-md border border-amber-800 bg-amber-950/40 px-3 py-2 text-sm text-amber-300"
          >
            <span>Prefilled from a link. Review it before sending.</span>
            <button
              type="button"
              onClick={() => setLinkPrefilled(false)}
              aria-label="Dismiss prefill notice"
              className="shrink-0 rounded p-0.5 text-amber-400 hover:bg-amber-900/50 hover:text-amber-200"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        <ChatInput
          large
          onSend={handleSend}
          disabled={customInvalid}
          placeholder="Describe what you want to investigate or analyse…"
          sessionId={sessionId ?? undefined}
          onBeforeAttach={() => ensureSession()}
          tourAnchor={tourAnchor}
          modelControl={
            config && (config.available_models.length > 1 || config.engines.length > 1) ? (
              <ModelEnginePicker
                config={config}
                model={selectedModel}
                engine={selectedEngine}
                onChange={(model, engine) => {
                  setSelectedModel(model);
                  setSelectedEngine(engine);
                }}
                menuClassName="bottom-full mb-1 right-0 w-96 max-w-[calc(100vw-2rem)]"
                title="Model for this conversation"
              />
            ) : undefined
          }
          connectorControls={
            sessionId ? (
              // Session exists → the status-aware panel drives grants + hosts the modals.
              <ConnectorPanel
                sessionId={sessionId}
                autoStart={autoStart}
                layout="icons"
                autoOpenMcp={mcpAutoOpen}
              />
            ) : (
              // Pre-session: clicking an icon lazily creates the session, then the
              // ConnectorPanel above takes over (autoStart / autoOpenMcp resume the flow).
              <>
                {/* Single tour anchor around both connector icons — one unambiguous
                    rect for the onboarding spotlight. Same gap as the toolbar row
                    (gap-1.5), so wrapping is layout-neutral. */}
                <span data-tour="connectors" className="flex items-center gap-1.5">
                  {PRESESSION_CONNECTORS.map(({ name, label, Icon }) => (
                    <ConnectorIconButton
                      key={name}
                      Icon={Icon}
                      title={`Give ${label} access`}
                      tone="off"
                      onClick={() => handleGiveAccess(name)}
                      disabled={customInvalid}
                      overlayTitle={label}
                      overlayBadge={<StateBadge state="not_connected" />}
                      overlayBody={<p className="text-gray-400">No access granted. Click to connect.</p>}
                    />
                  ))}
                </span>
                {/* Explicit, single tour anchor for the MCP icon — data-tour lives here,
                    not inside McpPanel (which renders in several places), so the tour
                    matches exactly this one element. */}
                <span data-tour="mcp" className="flex shrink-0">
                  <McpPanel
                    sessionId=""
                    variant="icon"
                    onBeforeOpen={handleConfigureMcp}
                    deferOpen
                  />
                </span>
              </>
            )
          }
        />
      </div>
    </div>
  );
}
