import { useEffect, useRef, useState } from 'react';
import { ExternalLink, Loader2, RotateCcwKey } from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { useConnectorsStore } from '@/store/connectors';
import { ConnectorName } from '@/types/connectors';
import { AwsAccountRoleModal } from './AwsAccountRoleModal';
import { AwsEnumProgressBar } from './AwsEnumProgressBar';
import { useAwsEnumProgress, EnumProgress } from '@/hooks/useAwsEnumProgress';
import { ConnectorPermissions } from './ConnectorPermissions';
import { McpPanel } from './McpPanel';
import { LABELS, ICONS, connectorTone } from './connectorChrome';
import { ConnectorIconButton } from './ConnectorIconButton';
import { StateBadge } from './StateBadge';
import { useConnectorGrant } from './useConnectorGrant';
import { cn } from '@/lib/utils';

interface Props {
  sessionId: string;
  // When set, kick off this connector's grant once its status has loaded.
  // Used by the start screen: granting access before the first message
  // silently creates the session, then hands off to this panel.
  autoStart?: ConnectorName | null;
  // 'inline' (default) packs connectors on one wrapping row (in-conversation header).
  // 'stacked' puts each connector on its own full-width row (start screen).
  // 'icons' renders each connector as a single square toolbar icon (composer bar):
  // colour = grant state, click = the same "Give access" flow. Hosts the same modals.
  layout?: 'inline' | 'stacked' | 'icons';
  // Open the MCP picker on mount — used by the start screen after it lazily
  // creates the session from the MCP row (which remounts this panel).
  autoOpenMcp?: boolean;
}

// Per-session "Give access" panel for external connectors. Each button is a
// bounded act of consent; expired credentials prompt a re-grant.
export function ConnectorPanel({ sessionId, autoStart, layout = 'inline', autoOpenMcp = false }: Props) {
  const stacked = layout === 'stacked';
  const iconsLayout = layout === 'icons';
  const {
    statuses,
    fetchStatus,
    pollAwsDevice,
    openAwsVerification,
    closeAwsVerification,
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
  const { grant, busy, error: startError, errorConnector } = useConnectorGrant(sessionId);
  const [renewing, setRenewing] = useState(false);
  const [awsModalOpen, setAwsModalOpen] = useState(false);
  // Once the user approves, the still-pending poll blocks on the slow account
  // enumeration — poll its progress so the modal shows accounts being retrieved.
  const liveEnumProgress = useAwsEnumProgress(sessionId, awsDevice?.status === 'pending');
  // Latch the last seen progress: the backend clears its registry entry a beat
  // before the approving poll returns, so reading `liveEnumProgress` directly would
  // briefly flash the (now useless) verification code back up in that gap. Hold the
  // last value until the device modal closes (awsDevice → null).
  const [enumProgress, setEnumProgress] = useState<EnumProgress | null>(null);
  // Close the popup exactly once per device flow: liveEnumProgress changes identity
  // on every poll tick, so without this guard the effect would re-fire each second.
  const popupClosedRef = useRef(false);
  useEffect(() => {
    if (liveEnumProgress) {
      setEnumProgress(liveEnumProgress);
      // Enumeration started ⇒ the code was just validated. Close the AWS popup now
      // rather than waiting for the approving poll to return (it blocks on the fetch).
      if (!popupClosedRef.current) {
        popupClosedRef.current = true;
        closeAwsVerification();
      }
    }
  }, [liveEnumProgress, closeAwsVerification]);
  useEffect(() => {
    if (!awsDevice) {
      setEnumProgress(null);
      popupClosedRef.current = false; // re-arm for the next device flow
    }
  }, [awsDevice]);

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

  // Auto-start a grant requested before the session existed (start screen).
  // Wait until the connector's status has loaded so grant() branches correctly.
  // Kept above the empty-status early return so the hook order stays stable.
  const autoStartedRef = useRef(false);
  useEffect(() => {
    if (autoStart && !autoStartedRef.current && statuses[autoStart]) {
      autoStartedRef.current = true;
      void grant(autoStart);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStart, statuses]);

  const connectors = Object.keys(LABELS) as ConnectorName[];
  // Only render connectors the backend reports (enabled ones).
  const visible = connectors.filter((c) => statuses[c]);
  // No connectors enabled, non-stacked, non-icons: nothing to show. The icons layout
  // still renders (the MCP icon may be present even with no AWS/GitLab connectors).
  if (visible.length === 0 && !stacked && !iconsLayout) return null;

  return (
    <div className={iconsLayout ? 'contents' : cn('px-4 py-2 bg-gray-900/60', !stacked && 'border-b border-gray-800')}>
      {iconsLayout ? (
        // Composer-toolbar icons: one square per enabled connector + the MCP icon.
        <div className="flex items-center gap-1.5">
          {visible.map((c) => {
            const status = statuses[c];
            const connected = status.state === 'connected';
            const failed = errorConnector === c;
            const label = failed
              ? `${LABELS[c]} — ${startError}`
              : connected
                ? `${LABELS[c]} — connected, click to revoke`
                : reauthRequired === c
                  ? `${LABELS[c]} — click to reauthorize`
                  : `Give ${LABELS[c]} access`;
            return (
              <div key={c} className="flex items-center gap-1">
                <ConnectorIconButton
                  Icon={ICONS[c]}
                  title={label}
                  // A failed grant flags the icon red (the busy spinner just stopped,
                  // so without this the failure would be invisible) with the error text.
                  tone={failed ? 'error' : connectorTone(status.state, reauthRequired === c)}
                  // Toggle: a granted connector revokes, otherwise (re)start the grant flow.
                  onClick={() => (connected ? revoke(sessionId, c) : grant(c))}
                  busy={busy === c}
                  overlayTitle={LABELS[c]}
                  overlayBadge={<StateBadge state={status.state} expiresAt={status.expires_at} />}
                  overlayBody={<ConnectorPermissions status={status} />}
                />
                {failed && (
                  <span className="max-w-[14rem] truncate text-xs text-red-400" title={startError ?? undefined}>
                    {startError}
                  </span>
                )}
              </div>
            );
          })}
          <McpPanel sessionId={sessionId} variant="icon" autoOpen={autoOpenMcp} showWhenEmpty />
          {canRenew && (
            <ConnectorIconButton
              Icon={RotateCcwKey}
              title="Renew access — re-grant the connectors this session previously had"
              tone="attention"
              onClick={handleRenew}
              busy={renewing}
              overlayTitle="Renew access"
              overlayBody={
                <p className="text-gray-400">
                  Re-grant the connectors this session previously had (reuses your AWS account/role
                  selection).
                </p>
              }
            />
          )}
        </div>
      ) : (
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
                  onClick={() => grant(c)}
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
        {/* MCP selector: connector-style item in both start and chat layouts. */}
        {stacked ? (
          <McpPanel sessionId={sessionId} variant="row" autoOpen={autoOpenMcp} showWhenEmpty />
        ) : (
          <McpPanel sessionId={sessionId} variant="inline" />
        )}
      </div>
      )}

      {/* AWS device authorization — our own centered modal (the AWS page itself
          opens in a tab/popup; it can't be embedded). */}
      <Modal
        isOpen={!!awsDevice}
        onClose={clearAwsDevice}
        title="AWS authorization"
        size="sm"
      >
        {awsDevice?.status === 'error' ? (
          <div className="space-y-4">
            <p className="text-red-400 text-sm">
              AWS authorization failed{awsDevice.detail ? `: ${awsDevice.detail}` : ''}.
            </p>
            <div className="flex justify-end">
              <Button variant="ghost" onClick={clearAwsDevice}>
                Close
              </Button>
            </div>
          </div>
        ) : (
          awsDevice && (
            <div className="space-y-4 text-sm text-gray-300">
              {awsRenewProfiles.length > 0 && (
                <div className="rounded-md border border-gray-700 bg-gray-800/60 p-2">
                  <div className="text-xs text-gray-400 mb-1">Renewed access:</div>
                  <ul className="space-y-0.5">
                    {awsRenewProfiles.map((p) => (
                      <li key={p.name} className="text-xs text-gray-200">
                        {p.account_name} <span className="text-gray-500">·</span> {p.role_name}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {/* Once enumeration starts, approval is already done — swap the code
                  prompt for the account-retrieval progress. */}
              {!enumProgress && (
                <>
                  <ol className="list-decimal space-y-1 pl-5">
                    <li>Open the AWS verification page.</li>
                    <li>Check that the code shown matches.</li>
                    <li>Approve.</li>
                  </ol>
                  <div className="text-center">
                    <div className="text-xs text-gray-500 mb-1">Verification code</div>
                    <div className="font-mono text-2xl tracking-widest text-gray-100">
                      {awsDevice.userCode}
                    </div>
                  </div>
                  <Button onClick={openAwsVerification} className="w-full">
                    <ExternalLink className="h-4 w-4 mr-2" />
                    Open the AWS page
                  </Button>
                </>
              )}
              {enumProgress && <AwsEnumProgressBar progress={enumProgress} />}
              <div className="flex items-start justify-between gap-2">
                <div className="text-xs text-gray-500">
                  {enumProgress ? (
                    'Access approved. Retrieving your accounts…'
                  ) : (
                    <>
                      Waiting for approval…
                      <div>This window will close automatically once access is granted.</div>
                    </>
                  )}
                </div>
                <button
                  onClick={clearAwsDevice}
                  className="shrink-0 text-xs text-gray-400 underline hover:text-gray-200"
                >
                  Cancel
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
