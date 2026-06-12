import { Loader2 } from 'lucide-react';
import { useConnectorsStore } from '@/store/connectors';
import { ConnectorName } from '@/types/connectors';
import { LABELS, ICONS } from '@/components/connector/connectorChrome';
import { StateBadge } from '@/components/connector/StateBadge';
import { useConnectorGrant } from '@/components/connector/useConnectorGrant';

interface Props {
  sessionId: string;
}

// In-conversation grant affordance. The header ConnectorPanel always
// lists every connector and owns Revoke for active grants; this notice appears in
// the message flow only when a connector actually needs attention mid-conversation
// — either a tool just signalled it needs one (reauthRequired, from the
// connector_required WS event) or a previously-held grant has expired. A connected
// connector is never an in-flow interruption, so Revoke isn't offered here. It
// reuses the exact consent flow and live status the panel uses (the AWS device /
// picker modals stay owned by the always-mounted ConnectorPanel), so granting here
// is identical to granting there.
export function ConnectorInlineNotice({ sessionId }: Props) {
  const { statuses, reauthRequired } = useConnectorsStore();
  const { grant, busy, error } = useConnectorGrant(sessionId);

  // Surface connectors a tool just asked for, plus any whose held grant expired.
  const needing = (Object.keys(LABELS) as ConnectorName[]).filter((c) => {
    const status = statuses[c];
    if (!status) return false; // connector disabled on the backend
    if (c === reauthRequired && status.state !== 'connected') return true;
    return status.state === 'expired';
  });

  if (needing.length === 0) return null;

  return (
    // Sticky to the top of the scroll viewport so the prompt stays visible when a
    // tool raises it mid-conversation while the user is scrolled to the bottom.
    <div
      className="sticky top-0 z-10 mb-4 space-y-2 bg-gray-900/90 backdrop-blur-sm"
      data-testid="connector-inline-notice"
    >
      {needing.map((c) => {
        const status = statuses[c];
        const Icon = ICONS[c];
        const expired = status.state === 'expired';
        return (
          <div
            key={c}
            className="flex flex-wrap items-center gap-2 rounded-md border border-yellow-700/60 bg-yellow-900/20 px-3 py-2"
          >
            <Icon className="h-4 w-4 text-yellow-400" />
            <span className="text-sm text-gray-200">
              {expired
                ? `${LABELS[c]} access expired`
                : `This step needs ${LABELS[c]} access`}
            </span>
            <StateBadge state={status.state} expiresAt={status.expires_at} />
            <div className="ml-auto flex items-center gap-3">
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
                ) : status.state === 'needs_selection' ? (
                  'Choose account'
                ) : (
                  'Give access'
                )}
              </button>
            </div>
          </div>
        );
      })}
      {error && (
        <div
          role="alert"
          className="rounded-md border border-red-700/60 bg-red-900/20 px-3 py-2 text-xs text-red-300"
        >
          {error}
        </div>
      )}
    </div>
  );
}
