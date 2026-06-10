import { useEffect, useRef, useState } from 'react';
import { Cloud, Gitlab, ExternalLink, Loader2 } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { useConnectorsStore } from '@/store/connectors';
import { ConnectorName, ConnectorState } from '@/types/connectors';
import { AwsAccountRoleModal } from './AwsAccountRoleModal';
import { ConnectorPermissions } from './ConnectorPermissions';
import { McpPanel } from './McpPanel';
import { cn } from '@/lib/utils';

const LABELS: Record<ConnectorName, string> = {
  aws: 'AWS',
  gitlab: 'GitLab',
};

const ICONS: Record<ConnectorName, typeof Cloud> = {
  aws: Cloud,
  gitlab: Gitlab,
};

function expiryLabel(expiresAt?: string | null): string | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return 'expired';
  const mins = Math.round(ms / 60000);
  return mins >= 60 ? `${Math.floor(mins / 60)}h${mins % 60}m left` : `${mins}m left`;
}

function StateBadge({ state, expiresAt }: { state: ConnectorState; expiresAt?: string | null }) {
  if (state === 'connected') {
    const exp = expiryLabel(expiresAt);
    return <Badge variant="success">connected{exp ? ` · ${exp}` : ''}</Badge>;
  }
  if (state === 'needs_selection') return <Badge variant="info">choose account</Badge>;
  if (state === 'expired') return <Badge variant="warning">expired</Badge>;
  if (state === 'error') return <Badge variant="danger">error</Badge>;
  return <Badge variant="default">not connected</Badge>;
}

interface Props {
  sessionId: string;
  // When set, kick off this connector's grant once its status has loaded.
  // Used by the start screen: granting access before the first message
  // silently creates the session, then hands off to this panel.
  autoStart?: ConnectorName | null;
  // 'inline' (default) packs connectors on one wrapping row (in-conversation header).
  // 'stacked' puts each connector on its own full-width row (start screen).
  layout?: 'inline' | 'stacked';
  // Open the MCP picker on mount — used by the start screen after it lazily
  // creates the session from the MCP row (which remounts this panel).
  autoOpenMcp?: boolean;
}

// Per-session "Give access" panel for external connectors. Each button is a
// bounded act of consent; expired credentials prompt a re-grant.
export function ConnectorPanel({ sessionId, autoStart, layout = 'inline', autoOpenMcp = false }: Props) {
  const stacked = layout === 'stacked';
  const {
    statuses,
    fetchStatus,
    initiate,
    startAwsDevice,
    pollAwsDevice,
    openAwsVerification,
    clearAwsDevice,
    awsDevice,
    awsModalRequested,
    clearAwsModalRequest,
    revoke,
    reauthRequired,
    renewable,
    fetchRenewable,
    renewAll,
    awsAutoSelect,
  } = useConnectorsStore();
  const [busy, setBusy] = useState<ConnectorName | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [renewing, setRenewing] = useState(false);
  const [awsModalOpen, setAwsModalOpen] = useState(false);

  useEffect(() => {
    if (sessionId) {
      fetchStatus(sessionId);
      fetchRenewable(sessionId);
    }
  }, [sessionId, fetchStatus, fetchRenewable]);

  const handleRenew = async () => {
    setRenewing(true);
    try {
      await renewAll(sessionId);
    } finally {
      setRenewing(false);
    }
  };

  // Offer renewal only for previously-granted connectors that aren't currently connected.
  const canRenew = renewable.some((g) => statuses[g.connector]?.state !== 'connected');

  // While renewing AWS, the device modal shows which accounts/roles will be
  // re-applied (the saved selection) so the user confirms what's being renewed.
  const awsRenewProfiles =
    awsDevice && awsAutoSelect
      ? (renewable.find((g) => g.connector === 'aws')?.aws_selection?.profiles ?? [])
      : [];

  // Open the AWS selection modal only right after device auth completes (explicit
  // flag) — NOT on every session load from a possibly-stale status.
  useEffect(() => {
    if (awsModalRequested) {
      setAwsModalOpen(true);
      clearAwsModalRequest();
    }
  }, [awsModalRequested, clearAwsModalRequest]);

  // While the AWS device authorization is pending, poll — serialized (wait for each
  // response before scheduling the next) to avoid redeeming the device code twice.
  useEffect(() => {
    if (awsDevice?.status !== 'pending') return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      let status = 'pending';
      try {
        status = await pollAwsDevice(sessionId);
      } catch {
        status = 'pending'; // transient error → keep polling
      }
      if (!cancelled && status === 'pending') timer = setTimeout(tick, 3000);
    };
    timer = setTimeout(tick, 3000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [awsDevice?.status, sessionId, pollAwsDevice]);

  const handleClick = async (connector: ConnectorName) => {
    const status = statuses[connector];
    if (connector === 'aws' && status?.state === 'needs_selection') {
      setAwsModalOpen(true);
      return;
    }
    setBusy(connector);
    setStartError(null);
    try {
      if (connector === 'aws') {
        await startAwsDevice(sessionId); // device grant: shows code + URL, then polls
      } else {
        await initiate(sessionId, connector); // OAuth redirect (GitLab)
      }
    } catch (e) {
      const detail = e instanceof Error && e.message ? `: ${e.message}` : '';
      setStartError(`Failed to start ${LABELS[connector]} authorization${detail}`);
    } finally {
      setBusy(null);
    }
  };

  // Auto-start a grant requested before the session existed (start screen).
  // Wait until the connector's status has loaded so handleClick branches correctly.
  // Kept above the empty-status early return so the hook order stays stable.
  const autoStartedRef = useRef(false);
  useEffect(() => {
    if (autoStart && !autoStartedRef.current && statuses[autoStart]) {
      autoStartedRef.current = true;
      void handleClick(autoStart);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStart, statuses]);

  const connectors = Object.keys(LABELS) as ConnectorName[];
  // Only render connectors the backend reports (enabled ones).
  const visible = connectors.filter((c) => statuses[c]);
  if (visible.length === 0 && !stacked) return null;

  return (
    <div className={cn('px-4 py-2 bg-gray-900/60', !stacked && 'border-b border-gray-800')}>
      <div className={cn('flex gap-2', stacked ? 'flex-col items-stretch' : 'items-center flex-wrap')}>
        <span className={cn('text-xs text-gray-500', stacked ? 'mb-0.5' : 'mr-1')}>External access:</span>
        {visible.map((c) => {
          const status = statuses[c];
          const Icon = ICONS[c];
          const connected = status.state === 'connected';
          const highlight = reauthRequired === c;
          return (
            <div
              key={c}
              className={cn(
                'group relative flex items-center gap-2 rounded-md px-2 py-1',
                highlight && 'ring-1 ring-yellow-600',
                stacked && 'w-full justify-between',
              )}
            >
              <div className="flex items-center gap-2">
                <Icon className="h-4 w-4 text-gray-400" />
                <span className="text-sm text-gray-200">{LABELS[c]}</span>
                <StateBadge state={status.state} expiresAt={status.expires_at} />
              </div>

              {/* Hover popup: what this connector currently grants (secret-free). */}
              <div className="pointer-events-none absolute left-0 top-full z-50 mt-1 hidden w-60 rounded-md border border-gray-700 bg-gray-800 p-2 text-xs shadow-lg group-hover:block">
                <div className="mb-1 font-medium text-gray-300">Permissions</div>
                <ConnectorPermissions status={status} />
              </div>
              {connected ? (
                <button
                  onClick={() => revoke(sessionId, c)}
                  className="text-xs text-gray-400 hover:text-red-400"
                >
                  Revoke
                </button>
              ) : (
                <button
                  onClick={() => handleClick(c)}
                  disabled={busy === c}
                  className="text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50"
                >
                  {busy === c ? (
                    <span role="status" className="flex items-center gap-1">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      Waiting for {LABELS[c]}…
                    </span>
                  ) : (
                    `Give access`
                  )}
                </button>
              )}
            </div>
          );
        })}
        {canRenew && (
          <button
            onClick={handleRenew}
            disabled={renewing}
            className="ml-1 text-xs px-2 py-1 rounded-md border border-blue-700 text-blue-300 hover:bg-blue-900/30 disabled:opacity-50"
            title="Re-grant the connectors this session previously had (reuses your AWS account/role selection)"
          >
            {renewing ? 'Renewing…' : 'Renew access'}
          </button>
        )}
        {startError && <span className="text-xs text-red-400">{startError}</span>}
        {/* MCP selector: a connector-style row in the stacked layout, a compact
            button (after a divider) on the in-conversation bar. */}
        {stacked ? (
          <McpPanel sessionId={sessionId} variant="row" autoOpen={autoOpenMcp} showWhenEmpty />
        ) : (
          <>
            <span className="mx-1 h-5 w-px bg-gray-700" aria-hidden />
            <McpPanel sessionId={sessionId} variant="inline" />
          </>
        )}
      </div>

      {/* AWS device authorization — our own centered modal (the AWS page itself
          opens in a tab/popup; it can't be embedded). */}
      <Modal
        isOpen={!!awsDevice}
        onClose={clearAwsDevice}
        title="Autorisation AWS"
        size="sm"
      >
        {awsDevice?.status === 'error' ? (
          <div className="space-y-4">
            <p className="text-red-400 text-sm">
              Échec de l'autorisation AWS{awsDevice.detail ? ` : ${awsDevice.detail}` : ''}.
            </p>
            <div className="flex justify-end">
              <Button variant="ghost" onClick={clearAwsDevice}>
                Fermer
              </Button>
            </div>
          </div>
        ) : (
          awsDevice && (
            <div className="space-y-4 text-sm text-gray-300">
              {awsRenewProfiles.length > 0 && (
                <div className="rounded-md border border-gray-700 bg-gray-800/60 p-2">
                  <div className="text-xs text-gray-400 mb-1">Accès renouvelé&nbsp;:</div>
                  <ul className="space-y-0.5">
                    {awsRenewProfiles.map((p) => (
                      <li key={p.name} className="text-xs text-gray-200">
                        {p.account_name} <span className="text-gray-500">·</span> {p.role_name}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <p>
                1. Ouvre la page de vérification AWS, 2. vérifie que le code affiché
                correspond, puis approuve.
              </p>
              <div className="text-center">
                <div className="text-xs text-gray-500 mb-1">Code de vérification</div>
                <div className="font-mono text-2xl tracking-widest text-gray-100">
                  {awsDevice.userCode}
                </div>
              </div>
              <Button onClick={openAwsVerification} className="w-full">
                <ExternalLink className="h-4 w-4 mr-2" />
                Ouvrir la page AWS
              </Button>
              <div className="flex items-start justify-between gap-2">
                <div className="text-xs text-gray-500">
                  En attente d'approbation…
                  <div>Cette fenêtre se fermera automatiquement une fois l'accès accordé.</div>
                </div>
                <button
                  onClick={clearAwsDevice}
                  className="shrink-0 text-xs text-gray-400 underline hover:text-gray-200"
                >
                  Annuler
                </button>
              </div>
            </div>
          )
        )}
      </Modal>

      <AwsAccountRoleModal
        sessionId={sessionId}
        isOpen={awsModalOpen}
        onClose={() => setAwsModalOpen(false)}
      />
    </div>
  );
}
