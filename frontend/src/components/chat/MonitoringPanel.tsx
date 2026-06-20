import { useMemo, ComponentType } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
} from 'recharts';
import { Activity, Cpu, MemoryStick, Network, HardDrive, Database } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { MetricSample, MetricsStatus } from '@/hooks/useSessionMetrics';

// conversation = blue, proxy = amber; the lighter shade is the paired series (tx / write).
const CONV = '#3b82f6';
const CONV_2 = '#93c5fd';
const PROXY = '#f59e0b';
const PROXY_2 = '#fcd34d';
const DISK = '#10b981';

function fmtBytes(n: number): string {
  if (!isFinite(n) || n <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 || v >= 100 ? 0 : 1)} ${units[i]}`;
}

const fmtRate = (n: number): string => `${fmtBytes(n)}/s`;
const fmtPct = (n: number): string => `${(isFinite(n) ? n : 0).toFixed(0)}%`;

interface Series {
  key: string;
  name: string;
  color: string;
}

interface ChartProps {
  title: string;
  icon: ComponentType<{ className?: string }>;
  data: Record<string, number | string | null>[];
  series: Series[];
  fmtY: (n: number) => string;
  refLine?: { y: number; label: string };
  yDomain?: [number | string, number | string];
}

function MetricChart({ title, icon: Icon, data, series, fmtY, refLine, yDomain }: ChartProps) {
  return (
    <Card variant="bordered" className="p-3">
      <div className="flex items-center gap-1.5 text-xs font-medium text-gray-300 mb-2">
        <Icon className="h-3.5 w-3.5 text-gray-400" />
        {title}
      </div>
      <ResponsiveContainer width="100%" height={170}>
        <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#374151" vertical={false} />
          <XAxis dataKey="time" tick={{ fontSize: 9, fill: '#6b7280' }} minTickGap={48} />
          <YAxis
            tick={{ fontSize: 9, fill: '#9ca3af' }}
            width={50}
            tickFormatter={fmtY}
            domain={yDomain ?? ['auto', 'auto']}
          />
          {refLine && (
            <ReferenceLine
              y={refLine.y}
              stroke="#6b7280"
              strokeDasharray="4 4"
              // The disk capacity sits far above usage — stretch the axis so it stays visible.
              ifOverflow="extendDomain"
              label={{ value: refLine.label, fill: '#9ca3af', fontSize: 9, position: 'insideTopRight' }}
            />
          )}
          <Tooltip
            contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 6, fontSize: 11 }}
            labelStyle={{ color: '#9ca3af' }}
            formatter={(value) => fmtY(Number(value))}
          />
          <Legend wrapperStyle={{ fontSize: 10 }} iconType="plainline" />
          {series.map((s) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stroke={s.color}
              dot={false}
              strokeWidth={1.5}
              isAnimationActive={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </Card>
  );
}

/**
 * Resource graphs for a conversation's sandbox container pair, rendered in the session's
 * "Monitoring" tab. Net/block-IO are cumulative counters from `docker stats`, so they're
 * differentiated into per-second rates here against each sample's client receive time.
 */
export function MonitoringPanel({ samples, status }: { samples: MetricSample[]; status: MetricsStatus }) {
  const data = useMemo(
    () =>
      samples.map((s, i) => {
        const prev = samples[i - 1];
        const dt = prev ? (s.t - prev.t) / 1000 : 0;
        // Cumulative counters reset to 0 when a container is rebuilt, so guard cur >= old.
        const rate = (cur?: number | null, old?: number | null) =>
          dt > 0 && cur != null && old != null && cur >= old ? (cur - old) / dt : 0;
        const memPct = (used?: number | null, limit?: number | null) =>
          used != null && limit != null && limit > 0 ? (used / limit) * 100 : null;
        return {
          time: new Date(s.t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
          convCpu: s.conv?.cpu_pct ?? null,
          proxyCpu: s.proxy?.cpu_pct ?? null,
          convMem: memPct(s.conv?.mem_bytes, s.conv?.mem_limit_bytes),
          proxyMem: memPct(s.proxy?.mem_bytes, s.proxy?.mem_limit_bytes),
          convNetRx: rate(s.conv?.net_rx_bytes, prev?.conv?.net_rx_bytes),
          convNetTx: rate(s.conv?.net_tx_bytes, prev?.conv?.net_tx_bytes),
          proxyNetRx: rate(s.proxy?.net_rx_bytes, prev?.proxy?.net_rx_bytes),
          proxyNetTx: rate(s.proxy?.net_tx_bytes, prev?.proxy?.net_tx_bytes),
          convBlkR: rate(s.conv?.blk_read_bytes, prev?.conv?.blk_read_bytes),
          convBlkW: rate(s.conv?.blk_write_bytes, prev?.conv?.blk_write_bytes),
          proxyBlkR: rate(s.proxy?.blk_read_bytes, prev?.proxy?.blk_read_bytes),
          proxyBlkW: rate(s.proxy?.blk_write_bytes, prev?.proxy?.blk_write_bytes),
          disk: s.disk_bytes ?? null,
        };
      }),
    [samples],
  );

  // Memory limit (--memory) is the same for both containers; take the latest non-zero one.
  const memLimit = useMemo(() => {
    for (let i = samples.length - 1; i >= 0; i--) {
      const lim = samples[i].conv?.mem_limit_bytes ?? samples[i].proxy?.mem_limit_bytes ?? 0;
      if (lim > 0) return lim;
    }
    return 0;
  }, [samples]);

  // Filesystem capacity behind the session dir — the disk chart's "full disk" ceiling.
  const diskTotal = useMemo(() => {
    for (let i = samples.length - 1; i >= 0; i--) {
      const total = samples[i].disk_total_bytes ?? 0;
      if (total > 0) return total;
    }
    return 0;
  }, [samples]);

  if (samples.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center p-6 min-h-0">
        <div className="flex items-center gap-2 max-w-md text-center text-sm text-gray-500">
          <Activity className="h-4 w-4 shrink-0" />
          <span>
            {status === 'disabled'
              ? 'Sandbox monitoring is disabled for this deployment.'
              : 'No active sandbox — resource graphs appear once a turn provisions the conversation containers.'}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-3 min-h-0">
      {status === 'not_running' && (
        <div className="mb-3 flex items-center gap-1.5 text-xs text-gray-500">
          <Activity className="h-3.5 w-3.5" />
          Sandbox idle — showing the last recorded window.
        </div>
      )}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        <MetricChart
          title="CPU"
          icon={Cpu}
          data={data}
          fmtY={fmtPct}
          series={[
            { key: 'convCpu', name: 'conversation', color: CONV },
            { key: 'proxyCpu', name: 'proxy', color: PROXY },
          ]}
        />
        <MetricChart
          title="Memory (% of limit)"
          icon={MemoryStick}
          data={data}
          fmtY={fmtPct}
          yDomain={[0, 100]}
          refLine={memLimit ? { y: 100, label: `limit ${fmtBytes(memLimit)}` } : undefined}
          series={[
            { key: 'convMem', name: 'conversation', color: CONV },
            { key: 'proxyMem', name: 'proxy', color: PROXY },
          ]}
        />
        <MetricChart
          title="Network I/O"
          icon={Network}
          data={data}
          fmtY={fmtRate}
          series={[
            { key: 'convNetRx', name: 'conv rx', color: CONV },
            { key: 'convNetTx', name: 'conv tx', color: CONV_2 },
            { key: 'proxyNetRx', name: 'proxy rx', color: PROXY },
            { key: 'proxyNetTx', name: 'proxy tx', color: PROXY_2 },
          ]}
        />
        <MetricChart
          title="Block I/O"
          icon={HardDrive}
          data={data}
          fmtY={fmtRate}
          series={[
            { key: 'convBlkR', name: 'conv read', color: CONV },
            { key: 'convBlkW', name: 'conv write', color: CONV_2 },
            { key: 'proxyBlkR', name: 'proxy read', color: PROXY },
            { key: 'proxyBlkW', name: 'proxy write', color: PROXY_2 },
          ]}
        />
        <MetricChart
          title="Disk usage (session dir)"
          icon={Database}
          data={data}
          fmtY={fmtBytes}
          refLine={diskTotal ? { y: diskTotal, label: `capacity ${fmtBytes(diskTotal)}` } : undefined}
          series={[{ key: 'disk', name: 'workdir + logs', color: DISK }]}
        />
      </div>
    </div>
  );
}
