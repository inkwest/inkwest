// Inkwest service worker: Web Push only (no caching, no offline).

// No fetch handler, so a new version can take over at once instead of waiting for every tab to close.
self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('push', (event) => {
  let msg;
  try {
    msg = event.data && event.data.json();
  } catch {
    msg = null;
  }
  // Only our backend can encrypt to this subscription, so a bad payload is a bug: show nothing,
  // accepting that Chrome may spend its silent-push budget and show its generic notice.
  if (!msg || msg.type !== 'share_invite') return;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      // A focused Inkwest window already shows the in-app invite popup.
      if (wins.some((w) => w.focused)) return;
      const who = msg.invited_by || 'Someone';
      const what = msg.session_title ? `“${msg.session_title}”` : 'a conversation';
      return self.registration.showNotification(`${who} shared ${what} with you`, {
        body: 'Open Inkwest to accept or decline.',
        tag: msg.share_id,
        data: { session_id: msg.session_id },
      });
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) =>
      // Focus without navigating: the invite popup appears on whatever page the user is on.
      wins.length ? wins[0].focus() : self.clients.openWindow('/'),
    ),
  );
});
