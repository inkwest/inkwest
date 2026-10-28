import { useState } from 'react';
import { Bell, X } from 'lucide-react';
import { isPushOptInSnoozed, snoozePushOptIn, usePushOptIn } from '@/hooks/usePushOptIn';

// Asks in-app first: a refused browser prompt cannot be raised again, so it only follows this click.
export function PushOptInBanner() {
  const { offer, permission, state, enable } = usePushOptIn();
  // Local fallback for when localStorage is unavailable and the snooze cannot persist.
  const [dismissedHere, setDismissedHere] = useState(false);

  // Granted but the server refused the subscription: keep the banner up to say so.
  const failedAfterGrant = state === 'error' && permission === 'granted';
  if (dismissedHere || isPushOptInSnoozed() || !(offer || failedAfterGrant)) return null;

  const snooze = () => {
    snoozePushOptIn();
    setDismissedHere(true);
  };

  const turnOn = async () => {
    if ((await enable()) === 'dismissed') setDismissedHere(true);
  };

  return (
    <div
      role="region"
      aria-label="Browser notifications"
      className="bg-gray-800 border-b border-gray-700 text-gray-200 px-4 py-2 flex items-center gap-2"
    >
      <Bell className="h-4 w-4 shrink-0 text-blue-400" />
      <span className="flex-1 text-sm">
        {failedAfterGrant ? (
          <span role="alert">
            Could not turn on notifications. Try again from Settings, Notifications.
          </span>
        ) : (
          'Get a browser notification when someone shares a conversation with you.'
        )}
      </span>
      {!failedAfterGrant && (
        <>
          <button
            onClick={() => void turnOn()}
            disabled={state === 'busy'}
            className="text-sm font-medium px-3 py-1 rounded-md bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50"
          >
            Turn on
          </button>
          <button onClick={snooze} className="text-sm px-3 py-1 rounded-md text-gray-300 hover:bg-gray-700">
            Not now
          </button>
        </>
      )}
      <button onClick={snooze} aria-label="Dismiss" className="text-gray-400 hover:text-gray-200">
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
