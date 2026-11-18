// `/#prompt=<text>` prefills the new-conversation composer and never sends. Captured at boot,
// before AuthGuard can bounce through the OIDC login (which lands back on a bare `/`):
// sessionStorage carries it across that same-tab round-trip.
const KEY = 'inkwest-link-prefill';
// Long enough for an OIDC login round-trip; older entries would surface out of context.
const PREFILL_MAX_AGE_MS = 10 * 60 * 1000;

export function captureLinkPrefill(): void {
  if (window.location.pathname !== '/') return;
  const params = new URLSearchParams(window.location.hash.slice(1));
  const prompt = params.get('prompt');
  if (prompt === null) return;
  if (prompt) {
    try {
      sessionStorage.setItem(KEY, JSON.stringify({ prompt, at: Date.now() }));
    } catch {
      // Storage blocked: the prefill is lost, the app still loads.
    }
  }
  params.delete('prompt');
  const rest = params.toString();
  window.history.replaceState(
    window.history.state,
    '',
    window.location.pathname + window.location.search + (rest ? `#${rest}` : ''),
  );
}

export function takeLinkPrefill(): string | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    sessionStorage.removeItem(KEY);
    if (raw === null) return null;
    const { prompt, at } = JSON.parse(raw);
    if (typeof prompt !== 'string' || typeof at !== 'number') return null;
    return Date.now() - at <= PREFILL_MAX_AGE_MS ? prompt : null;
  } catch {
    return null;
  }
}
