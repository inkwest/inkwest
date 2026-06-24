import { useConfigStore } from '@/store/config';
import { useSessionsStore } from '@/store/sessions';
import { ModelEnginePicker } from './ModelEnginePicker';

/**
 * Per-conversation model selector: shows the session's Engine·Model pairing and lets
 * the user change it on the fly (applies from the next message). The "Default" entry
 * un-pins the session so it follows the admin default again. Renders nothing until
 * config + a session load. Lives inline in the composer toolbar, so its dropdown
 * opens upward (`bottom-full`) to avoid overflowing past the bottom-anchored input.
 */
export function SessionModelSelect({ className }: { className?: string }) {
  const { config } = useConfigStore();
  const currentSession = useSessionsStore((s) => s.currentSession);
  const setSessionModel = useSessionsStore((s) => s.setSessionModel);

  if (!config || !currentSession) return null;

  return (
    <ModelEnginePicker
      config={config}
      model={currentSession.model ?? null}
      engine={currentSession.engine ?? null}
      allowDisabledCurrent
      className={className}
      menuClassName="bottom-full mb-1 right-0 w-96 max-w-[calc(100vw-2rem)]"
      title={`Provider: ${config.provider} · Model for this conversation`}
      onChange={(model, engine) => {
        // Best-effort: server rejected (e.g. model disabled after this tab loaded its
        // config) → the store keeps the previous value and the trigger snaps back;
        // server-side validation is the actual guardrail, an accepted UX cost.
        setSessionModel(currentSession.id, model, engine).catch(() => {});
      }}
    />
  );
}
