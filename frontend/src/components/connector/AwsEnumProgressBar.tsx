import { EnumProgress } from '@/hooks/useAwsEnumProgress';

// Count + thin bar shown while the SSO accounts are being enumerated (the slow part
// of an AWS grant/refresh). Renders nothing when there's no enumeration in flight.
export function AwsEnumProgressBar({ progress }: { progress: EnumProgress | null }) {
  if (!progress) return null;
  const { done, total } = progress;
  // total === 0: ListAccounts hasn't returned yet → indeterminate (pulsing full bar).
  const determinate = total > 0;
  const pct = determinate ? Math.round((done / total) * 100) : 100;
  return (
    <div className="space-y-1">
      <div className="text-xs text-gray-400">
        Récupération de vos comptes AWS… {done}/{determinate ? total : '?'}
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-800">
        <div
          className={`h-full rounded-full bg-blue-500 transition-[width] duration-300 ${
            determinate ? '' : 'animate-pulse'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
