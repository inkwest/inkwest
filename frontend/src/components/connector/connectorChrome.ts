import { Gitlab } from 'lucide-react';
import type { ComponentType } from 'react';
import { ConnectorName, ConnectorState } from '@/types/connectors';
import { AwsIcon } from '@/components/icons/BrandIcons';

// Shared connector chrome (labels, icons, expiry formatting) used by the header
// ConnectorPanel, the in-conversation ConnectorInlineNotice, and the StateBadge,
// so the surfaces stay visually and semantically in sync. Pure (no JSX) so it can
// be imported anywhere without tripping react-refresh.

export const LABELS: Record<ConnectorName, string> = {
  aws: 'AWS',
  gitlab: 'GitLab',
};

// Loose icon type so lucide stroke icons (Gitlab) and our fill-based brand marks
// (AwsIcon) both satisfy it — both are called as `<Icon className="…" />`.
type ConnectorIcon = ComponentType<{ className?: string }>;

export const ICONS: Record<ConnectorName, ConnectorIcon> = {
  aws: AwsIcon,
  gitlab: Gitlab,
};

// Three-state tone for the toolbar connector icons: granted (green), needs action
// (amber: expired / awaiting account choice / errored / re-auth required), or no
// access (gray). Pure so it's shared by the icon button and any future surface.
export type ConnectorTone = 'on' | 'attention' | 'off' | 'error';

export function connectorTone(state: ConnectorState, reauthRequired = false): ConnectorTone {
  if (reauthRequired) return 'attention';
  switch (state) {
    case 'connected':
      return 'on';
    case 'expired':
    case 'needs_selection':
    case 'error':
      return 'attention';
    default: // not_connected, pending_auth
      return 'off';
  }
}

// Icon glyph colour per tone (resting → hover). The brighten is driven by the parent
// button's hover (group/cbtn) — not the glyph's own :hover — so hovering anywhere in
// the square brightens the icon too, matching the button's bg highlight (and the
// attach/skills buttons, whose icons inherit the button's hover colour).
export const TONE_CLASS: Record<ConnectorTone, string> = {
  on: 'text-emerald-400 group-hover/cbtn:text-emerald-300',
  attention: 'text-amber-400 group-hover/cbtn:text-amber-300',
  off: 'text-gray-500 group-hover/cbtn:text-gray-300',
  error: 'text-red-400 group-hover/cbtn:text-red-300',
};

export function expiryLabel(expiresAt?: string | null): string | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (ms <= 0) return 'expired';
  const mins = Math.round(ms / 60000);
  return mins >= 60 ? `${Math.floor(mins / 60)}h${mins % 60}m left` : `${mins}m left`;
}
