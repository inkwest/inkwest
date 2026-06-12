import { Badge } from '@/components/ui/Badge';
import { ConnectorState } from '@/types/connectors';
import { expiryLabel } from './connectorChrome';

// Single source of truth for how a connector's lifecycle state renders, shared by
// the header ConnectorPanel and the in-conversation ConnectorInlineNotice.
export function StateBadge({
  state,
  expiresAt,
}: {
  state: ConnectorState;
  expiresAt?: string | null;
}) {
  if (state === 'connected') {
    const exp = expiryLabel(expiresAt);
    return <Badge variant="success">connected{exp ? ` · ${exp}` : ''}</Badge>;
  }
  if (state === 'needs_selection') return <Badge variant="info">choose account</Badge>;
  if (state === 'expired') return <Badge variant="warning">expired</Badge>;
  if (state === 'error') return <Badge variant="danger">error</Badge>;
  return <Badge variant="default">not connected</Badge>;
}
