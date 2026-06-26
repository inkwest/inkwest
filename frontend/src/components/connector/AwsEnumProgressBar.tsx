import { EnumProgress } from '@/hooks/useAwsEnumProgress';

// Per-phase caption. Same bar, three jobs: SSO enumeration (device/refresh), STS
// minting and EKS discovery (the two halves of a grant). Unknown phase falls back
// to a neutral label rather than rendering an empty string.
const PHASE_LABELS: Record<string, string> = {
  enumerating: 'Retrieving your AWS accounts…',
  granting: 'Creating access…',
  discovering: 'Discovering EKS clusters…',
  // Terminal phase the backend lingers on briefly after discovery settles; shown for
  // the frame(s) before the picker auto-closes so the bar reads complete, not unknown.
  done: 'EKS clusters discovered',
};

// Outlined "shell" container with the fill (+ count) building inside it as the work
// progresses. The caller mounts this only while a phase is in flight; the defensive
// null branch just collapses the fill to empty.
// total === 0: count not yet known → indeterminate (full sheened bar).
export function AwsEnumProgressBar({ progress }: { progress: EnumProgress | null }) {
  const determinate = !!progress && progress.total > 0;
  const pct = progress ? (determinate ? Math.round((progress.done / progress.total) * 100) : 100) : 0;
  const label = progress ? (PHASE_LABELS[progress.phase] ?? 'Processing…') : '';
  return (
    <div className="relative h-5 w-full overflow-hidden rounded-full border border-gray-600 bg-gray-900">
      <div
        className={`absolute inset-y-0 left-0 rounded-full bg-blue-500/70 transition-[width] duration-300 ${
          progress ? 'progress-sheen' : ''
        }`}
        style={{ width: `${pct}%` }}
      />
      {progress && (
        <span className="absolute inset-0 flex items-center justify-center text-[11px] font-medium text-gray-100">
          {label} {progress.done}/{determinate ? progress.total : '?'}
        </span>
      )}
    </div>
  );
}
