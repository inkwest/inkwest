import { type ReactNode } from 'react';

interface Props {
  /** White header title. */
  title: string;
  /** Optional badge shown next to the title (state badge, count, …). */
  badge?: ReactNode;
  /** Overlay body below the header. */
  body: ReactNode;
  /** The trigger (a toolbar button) the overlay hovers above. */
  children: ReactNode;
}

/**
 * Shared composer-toolbar hover card. Wraps a toolbar button and, on hover, overlays
 * a card above it (the composer sits at the viewport bottom, so it opens upward and
 * left-aligns to grow rightward). One source of truth so the connector icons, MCP,
 * attach and skills hovers all read identically. Info-only (pointer-events-none).
 */
export function ToolHoverCard({ title, badge, body, children }: Props) {
  return (
    <div className="group relative shrink-0">
      {children}
      <div className="pointer-events-none absolute bottom-full left-0 z-50 mb-2 hidden w-max min-w-[14rem] max-w-[24rem] rounded-md border border-gray-700 bg-gray-800 p-2 text-xs shadow-lg group-hover:block">
        <div className="mb-1 flex items-center gap-2 font-medium text-gray-100">
          <span>{title}</span>
          {badge}
        </div>
        {body}
      </div>
    </div>
  );
}
