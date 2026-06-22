import { EnumProgress } from '@/hooks/useAwsEnumProgress';

// Outlined "shell" container with the fill (+ count) building inside it as the SSO
// accounts are enumerated. The caller mounts this only while enumeration is in
// flight; the defensive null branch just collapses the fill to empty.
// total === 0: ListAccounts hasn't returned yet → indeterminate (full sheened bar).
export function AwsEnumProgressBar({ progress }: { progress: EnumProgress | null }) {
  const determinate = !!progress && progress.total > 0;
  const pct = progress ? (determinate ? Math.round((progress.done / progress.total) * 100) : 100) : 0;
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
          Récupération de vos comptes AWS… {progress.done}/{determinate ? progress.total : '?'}
        </span>
      )}
    </div>
  );
}
