import { useConfigStore } from '@/store/config';
import { useSessionsStore } from '@/store/sessions';
import { ModelEnginePicker } from './ModelEnginePicker';

/**
 * Per-conversation model selector: shows the session's Engine·Model pairing and lets
 * the user change it on the fly (applies from the next message). The "Default" entry
 * un-pins the session so it follows the admin default again. Renders nothing until
 * config + a session load.
 *
 * `asBar` renders it inside its own toolbar strip — used when the connector panel has
 * nothing else to show but the selector must stay reachable.
 */
export function SessionModelSelect({ className, asBar = false }: { className?: string; asBar?: boolean }) {
  const { config } = useConfigStore();
  const currentSession = useSessionsStore((s) => s.currentSession);
  const setSessionModel = useSessionsStore((s) => s.setSessionModel);

  if (!config || !currentSession) return null;

  const picker = (
    <ModelEnginePicker
      config={config}
      model={currentSession.model ?? null}
      engine={currentSession.engine ?? null}
      allowDisabledCurrent
      className={className}
      title={`Provider: ${config.provider} · Model for this conversation`}
      onChange={(model, engine) => {
        // Best-effort: server rejected (e.g. model disabled after this tab loaded its
        // config) → the store keeps the previous value and the trigger snaps back;
        // server-side validation is the actual guardrail, an accepted UX cost.
        setSessionModel(currentSession.id, model, engine).catch(() => {});
      }}
    />
  );

  if (asBar) {
    return (
      <div className="px-4 py-2 bg-gray-900/60 border-b border-gray-800 flex items-center justify-end">
        {picker}
      </div>
    );
  }
  return picker;
}
