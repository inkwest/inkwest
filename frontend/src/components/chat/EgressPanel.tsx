import { Globe, ShieldCheck, ShieldAlert } from 'lucide-react';
import { EgressEvent, isBlocked } from '@/hooks/useEgressSocket';
import { cn } from '@/lib/utils';

function EgressRow({ e }: { e: EgressEvent }) {
  const blocked = isBlocked(e);
  return (
    <div
      className={cn(
        'flex items-center gap-2 py-1 px-2 rounded text-xs',
        blocked && 'bg-red-900/20'
      )}
    >
      {blocked ? (
        <ShieldAlert className="h-3.5 w-3.5 text-red-400 shrink-0" />
      ) : (
        <ShieldCheck className="h-3.5 w-3.5 text-green-400 shrink-0" />
      )}
      <span className="flex-1 truncate font-mono text-gray-200">
        {e.host}{e.port ? `:${e.port}` : ''}
      </span>
      <span className={cn('shrink-0 text-[11px]', blocked ? 'text-red-400' : 'text-gray-500')}>
        {e.method}
      </span>
    </div>
  );
}

/**
 * Full-pane live network-egress audit for a conversation: every host the sandboxed
 * agent reached through its squid proxy, allowed (green) or blocked (red). Rendered
 * inside the conversation's "Network" tab.
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
