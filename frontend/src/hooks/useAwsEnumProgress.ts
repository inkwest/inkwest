import { useEffect, useState } from 'react';
import * as api from '@/services/api';

export interface EnumProgress {
  done: number;
  total: number;
}

// Polls AWS account-enumeration progress (~1s) while `active`. Returns null until
// the backend reports the "enumerating" phase, and resets to null when inactive —
// so callers render the progress bar only while accounts are actually being pulled.
export function useAwsEnumProgress(sessionId: string, active: boolean): EnumProgress | null {
  const [progress, setProgress] = useState<EnumProgress | null>(null);

  useEffect(() => {
    if (!active) {
      setProgress(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const p = await api.getAwsEnumProgress(sessionId);
        if (!cancelled) {
          setProgress(p.phase === 'enumerating' ? { done: p.done, total: p.total } : null);
        }
      } catch {
        // Transient (e.g. 409 before the grant lands) — keep polling.
      }
      if (!cancelled) timer = setTimeout(tick, 1000);
    };
    void tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [sessionId, active]);

  return progress;
}
