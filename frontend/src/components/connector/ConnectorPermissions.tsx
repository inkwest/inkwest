import { AwsSelectionProfile, ConnectorStatus } from '@/types/connectors';

// Secret-free detail of what a connector currently grants, shown in the hover popup
// on each connector chip. AWS lists the selected account/role profiles; other
// connectors show their scope label.
export function ConnectorPermissions({ status }: { status: ConnectorStatus }) {
  if (status.state === 'not_connected') {
    return <p className="text-gray-400">No access granted.</p>;
  }
  if (status.state === 'needs_selection') {
    return <p className="text-gray-400">Authenticated — choose an account/role.</p>;
  }
  if (status.state === 'error') {
    return <p className="text-red-400">{status.error || 'Connector error.'}</p>;
  }

  const profiles =
    (status.selection as { profiles?: AwsSelectionProfile[] } | null)?.profiles ?? [];

  return (
    <div className="space-y-1">
      {profiles.length > 0 && (
        <ul className="space-y-0.5">
          {profiles.map((p) => (
            <li key={p.name} className="text-gray-200">
              {p.account_name} <span className="text-gray-600">·</span> {p.role_name}
            </li>
          ))}
        </ul>
      )}
      {status.scope && <p className="text-gray-400">{status.scope}</p>}
      {profiles.length === 0 && !status.scope && <p className="text-gray-400">Connected.</p>}
    </div>
  );
}
