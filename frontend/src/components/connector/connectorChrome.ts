import { Cloud, Gitlab } from 'lucide-react';
import { ConnectorName } from '@/types/connectors';

// Shared connector chrome (labels, icons, expiry formatting) used by the header
// ConnectorPanel, the in-conversation ConnectorInlineNotice, and the StateBadge,
// so the surfaces stay visually and semantically in sync. Pure (no JSX) so it can
// be imported anywhere without tripping react-refresh.

export const LABELS: Record<ConnectorName, string> = {
  aws: 'AWS',
  gitlab: 'GitLab',
};

export const ICONS: Record<ConnectorName, typeof Cloud> = {
  aws: Cloud,
  gitlab: Gitlab,
};

export function expiryLabel(expiresAt?: string | null): string | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return 'expired';
  const mins = Math.round(ms / 60000);
  return mins >= 60 ? `${Math.floor(mins / 60)}h${mins % 60}m left` : `${mins}m left`;
}
