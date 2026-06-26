import { Globe, ShieldCheck, ShieldAlert, AlertTriangle } from 'lucide-react';
import { EgressEvent, isBlocked, isError } from '@/hooks/useEgressSocket';
import { Tooltip } from '@/components/ui';
import { cn } from '@/lib/utils';

// Render squid timestamps in the viewer's own timezone/locale (no hardcoded zone) so
// the time reads correctly wherever the operator is — the underlying instant is the
// absolute epoch from the log. Forced 24h for an unambiguous audit format. Built once.
const TIME_FMT = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

function EgressRow({ e }: { e: EgressEvent }) {
  const blocked = isBlocked(e);
  const errored = !blocked && isError(e);
  // ts is squid's epoch-seconds (float, ms precision); 0 means it failed to parse.
  const at = e.ts ? new Date(e.ts * 1000) : null;
  // Tooltip on the whole row so the explanation surfaces wherever the cursor lands.
  const detail = blocked
    ? 'Blocked by the squid proxy — host outside the egress allowlist'
    : errored
      ? 'Upstream error — squid forwarded the request; the destination (or the connection to it) failed'
      : 'Allowed through the egress proxy';
  return (
    <Tooltip
      content={detail}
      className={cn(
        'flex items-center gap-2 py-1 px-2 rounded text-xs',
        blocked && 'bg-red-900/20',
        errored && 'bg-amber-900/20'
      )}
    >
      {at && (
        <span className="shrink-0 font-mono text-[11px] text-gray-500 tabular-nums">
          {TIME_FMT.format(at)}
        </span>
      )}
      {blocked ? (
        // Squid policy block: the ACL refused this host.
        <ShieldAlert className="h-3.5 w-3.5 text-red-400 shrink-0" />
      ) : errored ? (
        // Upstream/connection error (e.g. TCP_TUNNEL/503) — not a block.
        <AlertTriangle className="h-3.5 w-3.5 text-amber-400 shrink-0" />
      ) : (
        <ShieldCheck className="h-3.5 w-3.5 text-green-400 shrink-0" />
      )}
      <span className="flex-1 min-w-0 truncate font-mono text-gray-200">
        {e.host}{e.port ? `:${e.port}` : ''}
      </span>
      {(blocked || errored) && (
        <span className={cn('w-44 shrink-0 text-[11px] font-mono', blocked ? 'text-red-400' : 'text-amber-400')}>
          {e.status} · {blocked ? 'proxy block' : 'upstream error'}
        </span>
      )}
      <span
        className={cn(
          'shrink-0 text-[11px]',
          blocked ? 'text-red-400' : errored ? 'text-amber-400' : 'text-gray-500'
        )}
      >
        {e.method}
      </span>
    </Tooltip>
  );
}

/**
 * Full-pane live network-egress audit for a conversation: every host the sandboxed
 * agent reached through its squid proxy, classified by outcome — allowed (green),
 * blocked by the egress policy (red), or an upstream/connection error that squid
 * forwarded but the destination failed (amber). Rendered inside the "Network" tab.
 */
export function EgressLog({ events, connected }: { events: EgressEvent[]; connected: boolean }) {
  return (
    <div className="flex-1 overflow-y-auto p-3 space-y-0.5 min-h-0">
      {events.length === 0 ? (
        <div className="flex items-center gap-2 p-2 text-sm text-gray-500">
          <Globe className="h-4 w-4 shrink-0" />
          {connected
            ? 'No egress yet — hosts the agent reaches appear here (HTTPS shows on connection close).'
            : 'Connecting to the egress stream…'}
        </div>
      ) : (
        events.map((e) => <EgressRow key={e._id} e={e} />)
      )}
    </div>
  );
}
