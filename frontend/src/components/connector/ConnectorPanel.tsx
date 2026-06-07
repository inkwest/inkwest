import { useEffect, useState } from 'react';
import { Cloud, Gitlab, Activity, ExternalLink } from 'lucide-react';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { useConnectorsStore } from '@/store/connectors';
import { ConnectorName, ConnectorState } from '@/types/connectors';
import { AwsAccountRoleModal } from './AwsAccountRoleModal';

const LABELS: Record<ConnectorName, string> = {
  aws: 'AWS',
  gitlab: 'GitLab',
  coralogix: 'Coralogix',
};

const ICONS: Record<ConnectorName, typeof Cloud> = {
  aws: Cloud,
  gitlab: Gitlab,
  coralogix: Activity,
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
}

// Per-session "Give access" panel for external connectors. Each button is a
// bounded act of consent; expired credentials prompt a re-grant.
export function ConnectorPanel({ sessionId }: Props) {
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
    activateCoralogix,
    revoke,
    reauthRequired,
  } = useConnectorsStore();
  const [busy, setBusy] = useState<ConnectorName | null>(null);
  const [awsModalOpen, setAwsModalOpen] = useState(false);

  useEffect(() => {
    if (sessionId) fetchStatus(sessionId);
  }, [sessionId, fetchStatus]);

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

  const connectors = Object.keys(LABELS) as ConnectorName[];
  // Only render connectors the backend reports (enabled ones).
  const visible = connectors.filter((c) => statuses[c]);
  if (visible.length === 0) return null;

  const handleClick = async (connector: ConnectorName) => {
    const status = statuses[connector];
    if (connector === 'aws' && status?.state === 'needs_selection') {
      setAwsModalOpen(true);
      return;
    }
    setBusy(connector);
    try {
      if (connector === 'coralogix') {
        await activateCoralogix(sessionId);
      } else if (connector === 'aws') {
        await startAwsDevice(sessionId); // device grant: shows code + URL, then polls
      } else {
        await initiate(sessionId, connector); // OAuth redirect (GitLab)
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="px-4 py-2 border-b border-gray-800 bg-gray-900/60">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-gray-500 mr-1">External access:</span>
        {visible.map((c) => {
          const status = statuses[c];
          const Icon = ICONS[c];
          const connected = status.state === 'connected';
          const highlight = reauthRequired === c;
          return (
            <div
              key={c}
              className={`flex items-center gap-2 rounded-md px-2 py-1 ${
                highlight ? 'ring-1 ring-yellow-600' : ''
              }`}
            >
              <Icon className="h-4 w-4 text-gray-400" />
              <span className="text-sm text-gray-200">{LABELS[c]}</span>
              <StateBadge state={status.state} expiresAt={status.expires_at} />
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
                  {busy === c ? '…' : `Give access`}
                </button>
              )}
            </div>
          );
        })}
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
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-500">En attente d'approbation…</span>
                <button
                  onClick={clearAwsDevice}
                  className="text-xs text-gray-400 underline hover:text-gray-200"
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
