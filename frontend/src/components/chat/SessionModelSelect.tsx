import { clsx } from 'clsx';
import { modelLabel } from '@/services/api';
import { useConfigStore } from '@/store/config';
import { useSessionsStore } from '@/store/sessions';

/**
 * Per-conversation model selector: shows the session's model and lets the user
 * change it on the fly (applies from the next message). The "Default" entry
 * un-pins the session so it follows the admin default again. Renders nothing
 * until the config is loaded and a session is active.
 *
 * `asBar` renders it inside its own toolbar strip — used when the connector
 * panel has nothing else to show but the selector must stay reachable.
 */
export function SessionModelSelect({ className, asBar = false }: { className?: string; asBar?: boolean }) {
  const { config } = useConfigStore();
  const currentSession = useSessionsStore((s) => s.currentSession);
  const setSessionModel = useSessionsStore((s) => s.setSessionModel);

  if (!config || !currentSession) return null;

  // A session pinned to a since-disabled model must stay displayable — but
  // it's marked "(disabled)": the backend ignores the stale pin at turn time
  // and runs the default instead.
  const options =
    currentSession.model && !config.available_models.some((m) => m.id === currentSession.model)
      ? [
          {
            id: currentSession.model,
            label: `${currentSession.model} (disabled)`,
            legacy: false,
          },
          ...config.available_models,
        ]
      : config.available_models;

  const select = (
    <select
      value={currentSession.model ?? ''}
      onChange={(e) => {
        // '' = un-pin: follow the global default from now on.
        setSessionModel(currentSession.id, e.target.value || null).catch(() => {
          // Best-effort: server rejected (e.g. model disabled after this tab
          // loaded its config — config is fetched once per SPA lifetime). The
          // store keeps the previous value, so the select snaps back; the
          // server-side validation is the actual guardrail, accepted UX cost.
        });
      }}
      className={clsx(
        'text-xs text-gray-400 bg-gray-800 border border-gray-700 rounded-full px-2.5 py-0.5',
        'max-w-48 cursor-pointer focus:outline-none focus:ring-1 focus:ring-blue-500',
        className
      )}
      title={`Provider: ${config.provider} · Model for this conversation`}
    >
      <option value="">Default — {modelLabel(config.available_models, config.model)}</option>
      {options.map((m) => (
        <option key={m.id} value={m.id}>
          {m.label}
        </option>
      ))}
    </select>
  );

  if (asBar) {
    return (
      <div className="px-4 py-2 bg-gray-900/60 border-b border-gray-800 flex items-center justify-end">
        {select}
      </div>
    );
  }
  return select;
}
