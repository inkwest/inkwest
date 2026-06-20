import { useEffect, useState } from 'react';
import { fetchAPI } from '@/services/api';

const POLL_INTERVAL = 3000;
const MAX_SAMPLES = 200; // ~10 min of history at the poll interval

export interface ContainerStats {
  cpu_pct: number;
  mem_bytes: number;
  mem_limit_bytes: number;
  net_rx_bytes: number; // cumulative since container start — rates derived client-side
  net_tx_bytes: number;
  blk_read_bytes: number;
  blk_write_bytes: number;
}

export type MetricsStatus = 'disabled' | 'not_running' | 'running';

export interface MetricsSnapshot {
  ts: number;
  status: MetricsStatus;
  conv: ContainerStats | null;
  proxy: ContainerStats | null;
  disk_bytes: number | null;
  disk_total_bytes: number | null; // filesystem capacity — the disk chart's ceiling
}

/** A snapshot stamped with its client receive time (ms) — the chart x-axis. */
export interface MetricSample extends MetricsSnapshot {
  t: number;
}

interface MetricsState {
  samples: MetricSample[];
  status: MetricsStatus;
}

/**
 * Poll a conversation's sandbox resource metrics (`/api/sessions/{id}/metrics`) and keep
 * a rolling in-memory window. Mirrors {@link useEgressSocket}'s always-on subscription so
 * the Monitoring tab's availability + history are ready the moment the user opens it.
 *
 * Only `running` snapshots are appended (a reaped/idle sandbox reports `not_running`,
 * which freezes the existing history rather than charting a gap of zeros).
 */
export function useSessionMetrics(sessionId: string | undefined): MetricsState {
  const [samples, setSamples] = useState<MetricSample[]>([]);
  const [status, setStatus] = useState<MetricsStatus>('not_running');

  useEffect(() => {
    if (!sessionId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setSamples([]);

    async function poll() {
      try {
        const snap = await fetchAPI<MetricsSnapshot>(`/api/sessions/${sessionId}/metrics`);
        if (stopped) return;
        setStatus(snap.status);
        if (snap.status === 'running') {
          setSamples((prev) => {
            const next = [...prev, { ...snap, t: Date.now() }];
            return next.length > MAX_SAMPLES ? next.slice(-MAX_SAMPLES) : next;
          });
        }
      } catch {
        // transient (auth refresh, daemon blip) — keep polling
      }
      if (!stopped) timer = setTimeout(poll, POLL_INTERVAL);
    }
    void poll();

    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId]);

  return { samples, status };
}
