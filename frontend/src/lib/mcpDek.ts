/**
 * Browser side of the MCP token-vault key custody (audit-doc Phase C).
 *
 * A non-extractable AES-GCM key is generated once per browser profile and stored in
 * IndexedDB (shared across tabs of the same origin+profile, never readable by JS/XSS
 * because it is non-extractable). It wraps the per-user data-encryption key (DEK) the
 * server uses to seal MCP OAuth token blobs:
 *
 *   - on connect the server sends the wrapped DEK; we unwrap it here and hand the raw DEK
 *     back over the (TLS) WebSocket so the server can decrypt token blobs while connected;
 *   - on first use the server sends a fresh DEK; we wrap it and return the wrapped form for
 *     the server to persist.
 *
 * The wrapping key never leaves the browser, so the server's stored {wrapped DEK,
 * ciphertext} is useless at rest. A different profile (no IndexedDB key) cannot unwrap and
 * must reconnect OAuth — by design.
 */

const DB_NAME = 'inkwest-mcp';
const STORE = 'keys';
const KEY_ID = 'dek-wrapping-key';
const IV_BYTES = 12;

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbGet(db: IDBDatabase, key: string): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const req = db.transaction(STORE, 'readonly').objectStore(STORE).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbPut(db: IDBDatabase, key: string, value: unknown): Promise<void> {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/** Get the profile's non-extractable wrapping key, generating + persisting it on first use. */
async function getWrappingKey(): Promise<CryptoKey> {
  const db = await openDb();
  const existing = await idbGet(db, KEY_ID);
  if (existing instanceof CryptoKey) return existing;
  // non-extractable: the raw bytes can never be read out, only used to encrypt/decrypt.
  const key = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, [
    'encrypt',
    'decrypt',
  ]);
  await idbPut(db, KEY_ID, key);
  return key;
}

function b64encode(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function b64decode(value: string): Uint8Array {
  const s = atob(value);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

/** Wrap a raw DEK (server-generated) with the profile key. Returns base64 ciphertext + iv. */
export async function wrapDek(dekB64: string): Promise<{ wrapped_dek: string; wrap_iv: string }> {
  const key = await getWrappingKey();
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: iv as BufferSource },
    key,
    b64decode(dekB64) as BufferSource
  );
  return { wrapped_dek: b64encode(new Uint8Array(ct)), wrap_iv: b64encode(iv) };
}

/**
 * Unwrap the server-held wrapped DEK with the profile key. Returns the raw DEK as base64,
 * or null when this profile cannot unwrap it (no/rotated key) — caller then resets.
 */
export async function unwrapDek(wrappedB64: string, ivB64: string): Promise<string | null> {
  try {
    const key = await getWrappingKey();
    const pt = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: b64decode(ivB64) as BufferSource },
      key,
      b64decode(wrappedB64) as BufferSource
    );
    return b64encode(new Uint8Array(pt));
  } catch {
    return null;
  }
}

/**
 * Compute the reply to a server `mcp_dek_*` message, or null if there's nothing to send.
 * Pure (no socket) so it is unit-testable: a challenge unwraps to a `supply` (or `reset` when
 * this profile can't unwrap), and a bootstrap wraps to a `wrapped`.
 */
export async function dekReply(data: {
  type?: string;
  wrapped_dek?: string;
  wrap_iv?: string;
  dek?: string;
}): Promise<Record<string, unknown> | null> {
  if (data.type === 'mcp_dek_challenge' && data.wrapped_dek) {
    const dek = await unwrapDek(data.wrapped_dek, data.wrap_iv ?? '');
    return dek ? { type: 'mcp_dek_supply', dek } : { type: 'mcp_dek_reset' };
  }
  if (data.type === 'mcp_dek_bootstrap' && data.dek) {
    return { type: 'mcp_dek_wrapped', ...(await wrapDek(data.dek)) };
  }
  return null;
}
