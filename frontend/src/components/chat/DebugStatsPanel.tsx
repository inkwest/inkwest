import { useState } from 'react';
import { ChevronDown, ChevronUp, Activity, Zap, Clock } from 'lucide-react';
import { cn } from '@/lib/utils';

interface SessionStats {
  totalInputTokens: number;
  totalOutputTokens: number;
  totalToolCalls: number;
  totalIterations: number;
}

interface MessageStats {
  iterations?: number;
  inputTokens?: number;
  outputTokens?: number;
  toolCalls?: number;
  latencyMs?: number;
}

interface Props {
  sessionStats?: SessionStats;
  lastMessageStats?: MessageStats;
  className?: string;
}

function formatNumber(n: number): string {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return n.toString();
}

function formatLatency(ms: number): string {
  if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
  return ms + 'ms';
}

// Rough cost estimation (adjust rates as needed)
function estimateCost(inputTokens: number, outputTokens: number): string {
  // Claude Sonnet pricing: $3/M input, $15/M output (approximate)
  const inputCost = (inputTokens / 1000000) * 3;
  const outputCost = (outputTokens / 1000000) * 15;
  const total = inputCost + outputCost;
  if (total < 0.01) return '<$0.01';
  return '$' + total.toFixed(2);
}

export function DebugStatsPanel({ sessionStats, lastMessageStats, className }: Props) {
  const [expanded, setExpanded] = useState(false);

  const hasStats = sessionStats || lastMessageStats;
  if (!hasStats) return null;

  const totalTokens = (sessionStats?.totalInputTokens || 0) + (sessionStats?.totalOutputTokens || 0);

  return (
    <div className={cn(
      'border border-gray-700 rounded-lg bg-gray-900/50 text-xs',
      className
    )}>
      {/* Collapsed summary bar */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-gray-800/50"
      >
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1 text-gray-400">
            <Activity className="h-3 w-3" />
            <span>{formatNumber(totalTokens)} tokens</span>
          </div>
          {sessionStats && (
            <div className="flex items-center gap-1 text-gray-400">
              <Zap className="h-3 w-3" />
              <span>{sessionStats.totalToolCalls} tools</span>
            </div>
          )}
          {lastMessageStats?.latencyMs && (
            <div className="flex items-center gap-1 text-gray-400">
              <Clock className="h-3 w-3" />
              <span>{formatLatency(lastMessageStats.latencyMs)}</span>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-green-400">
            {estimateCost(
              sessionStats?.totalInputTokens || 0,
              sessionStats?.totalOutputTokens || 0
            )}
          </span>
          {expanded ? (
            <ChevronUp className="h-4 w-4 text-gray-500" />
          ) : (
            <ChevronDown className="h-4 w-4 text-gray-500" />
          )}
        </div>
      </button>

      {/* Expanded details */}
      {expanded && (
        <div className="px-3 py-3 border-t border-gray-700 grid grid-cols-2 gap-4">
          {/* Session totals */}
          {sessionStats && (
            <div className="space-y-2">
              <div className="text-gray-500 font-medium uppercase tracking-wide">Session Totals</div>
              <div className="space-y-1">
                <div className="flex justify-between">
                  <span className="text-gray-400">Input tokens:</span>
                  <span className="text-blue-400 font-mono">{formatNumber(sessionStats.totalInputTokens)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-400">Output tokens:</span>
                  <span className="text-green-400 font-mono">{formatNumber(sessionStats.totalOutputTokens)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-400">Tool calls:</span>
                  <span className="text-yellow-400 font-mono">{sessionStats.totalToolCalls}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-400">Iterations:</span>
                  <span className="text-purple-400 font-mono">{sessionStats.totalIterations}</span>
                </div>
              </div>
            </div>
          )}

          {/* Last message stats */}
          {lastMessageStats && (
            <div className="space-y-2">
              <div className="text-gray-500 font-medium uppercase tracking-wide">Last Response</div>
              <div className="space-y-1">
                {lastMessageStats.inputTokens !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-gray-400">Input:</span>
                    <span className="text-blue-400 font-mono">{formatNumber(lastMessageStats.inputTokens)}</span>
                  </div>
                )}
                {lastMessageStats.outputTokens !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-gray-400">Output:</span>
                    <span className="text-green-400 font-mono">{formatNumber(lastMessageStats.outputTokens)}</span>
                  </div>
                )}
                {lastMessageStats.iterations !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-gray-400">Iterations:</span>
                    <span className="text-purple-400 font-mono">{lastMessageStats.iterations}</span>
                  </div>
                )}
                {lastMessageStats.latencyMs !== undefined && (
                  <div className="flex justify-between">
                    <span className="text-gray-400">Latency:</span>
                    <span className="text-orange-400 font-mono">{formatLatency(lastMessageStats.latencyMs)}</span>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
