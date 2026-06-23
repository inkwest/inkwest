import { useEffect, useState } from 'react';
import * as api from '@/services/api';

export interface EnumProgress {
  // "enumerating" (SSO account listing) | "granting" (STS minting) |
  // "discovering" (EKS cluster discovery). Drives the bar's label.
  phase: string;
  done: number;
  total: number;
}

// Polls AWS grant/enumeration progress (~1s) while `active`. Returns null until the
// backend reports a non-idle phase, and resets to null when inactive or once the
// backend goes idle again — so callers render the progress bar only while work is
// running, and can detect completion by the transition back to null.
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
          setProgress(p.phase !== 'idle' ? { phase: p.phase, done: p.done, total: p.total } : null);
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
