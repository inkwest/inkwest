/**
 * One-time migration cleanup.
 *
 * The app used to persist the session JWT in localStorage under 'session_token'.
 * Auth now rides a server-set HttpOnly cookie that JS can't read, so any value
 * still sitting in localStorage is a dead, XSS-readable credential left over
 * from a build before the migration. Purge it on boot. Idempotent — once the
 * key is gone this is a no-op, so it's safe to run on every load.
 */
const LEGACY_SESSION_KEY = 'session_token';

export function purgeLegacySessionToken(): void {
  try {
    localStorage.removeItem(LEGACY_SESSION_KEY);
  } catch {
    // localStorage may be unavailable (private mode, sandboxed iframe) — there
    // is nothing to purge in that case, so swallow and move on.
  }
}
