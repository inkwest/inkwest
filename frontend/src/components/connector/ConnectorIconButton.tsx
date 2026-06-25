import { type ComponentType, type ReactNode } from 'react';
import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { ToolHoverCard } from '@/components/chat/ToolHoverCard';
import { TONE_CLASS, type ConnectorTone } from './connectorChrome';

interface Props {
  Icon: ComponentType<{ className?: string }>;
  /** Accessible name + native tooltip (e.g. "AWS — connected, click to revoke"). */
  title: string;
  tone: ConnectorTone;
  onClick: () => void;
  disabled?: boolean;
  /** Grant in flight → spinner instead of the glyph. */
  busy?: boolean;
  /** Hover-card header title (e.g. "AWS"). When set, the icon gets a hover overlay. */
  overlayTitle?: string;
  /** Optional badge beside the overlay title (state badge, count, …). */
  overlayBadge?: ReactNode;
  /** Overlay body (granted permissions / enabled servers). */
  overlayBody?: ReactNode;
  /** Onboarding-tour anchor (data-tour) so the first-run tour can spotlight this icon. */
  dataTour?: string;
}

/**
 * One square toolbar button per external connector. Clicking runs the grant/revoke
 * flow wired by the caller; the glyph colour reflects grant state (green = connected,
 * amber = needs action, gray = no access). When overlay props are given it shows the
 * shared ToolHoverCard on hover, identical to the attach/skills/MCP hovers.
 */
export function ConnectorIconButton({
  Icon,
  title,
  tone,
  onClick,
  disabled,
  busy,
  overlayTitle,
  overlayBadge,
  overlayBody,
  dataTour,
}: Props) {
  const button = (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || busy}
      aria-label={title}
      title={title}
      data-tour={dataTour}
      className="group/cbtn flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-gray-600 bg-gray-700 hover:bg-gray-600 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {busy ? (
        <Loader2 className="h-4 w-4 animate-spin text-gray-300" />
      ) : (
        <Icon className={cn('h-4 w-4', TONE_CLASS[tone])} />
      )}
    </button>
  );

  if (!overlayTitle) return button;
  return (
    <ToolHoverCard title={overlayTitle} badge={overlayBadge} body={overlayBody}>
      {button}
    </ToolHoverCard>
  );
}
