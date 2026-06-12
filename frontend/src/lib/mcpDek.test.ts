// fake-indexeddb polyfills IndexedDB (jsdom has none); WebCrypto is provided by the runtime.
import 'fake-indexeddb/auto';
import { describe, it, expect } from 'vitest';
import { wrapDek, unwrapDek, dekReply } from './mcpDek';

// A server-issued DEK is 32 random bytes, base64-encoded.
function fakeDekB64(): string {
  const raw = crypto.getRandomValues(new Uint8Array(32));
  let s = '';
  for (const b of raw) s += String.fromCharCode(b);
  return btoa(s);
}

describe('mcpDek', () => {
  it('wraps then unwraps a DEK round-trip with the profile key', async () => {
    const dek = fakeDekB64();
    const { wrapped_dek, wrap_iv } = await wrapDek(dek);
    expect(wrapped_dek).not.toEqual(dek); // actually encrypted
    expect(await unwrapDek(wrapped_dek, wrap_iv)).toEqual(dek);
  });

  it('unwrap returns null on garbage (drives the reset path)', async () => {
    expect(await unwrapDek('bm90LXZhbGlk', 'YmFkaXY=')).toBeNull();
  });

  it('dekReply: bootstrap → wrapped, and that wrapped re-challenges back to the same DEK', async () => {
    const dek = fakeDekB64();
    const wrapped = await dekReply({ type: 'mcp_dek_bootstrap', dek });
    expect(wrapped?.type).toBe('mcp_dek_wrapped');

    const supply = await dekReply({
      type: 'mcp_dek_challenge',
      wrapped_dek: wrapped!.wrapped_dek as string,
      wrap_iv: wrapped!.wrap_iv as string,
    });
    expect(supply).toEqual({ type: 'mcp_dek_supply', dek });
  });

  it('dekReply: a challenge this profile cannot unwrap yields a reset', async () => {
    const reply = await dekReply({
      type: 'mcp_dek_challenge',
      wrapped_dek: 'bm90LXZhbGlk',
      wrap_iv: 'YmFkaXY=',
    });
    expect(reply).toEqual({ type: 'mcp_dek_reset' });
  });

  it('dekReply ignores unrelated messages', async () => {
    expect(await dekReply({ type: 'mcp_dek_ready' })).toBeNull();
    expect(await dekReply({ type: 'mcp_dek_absent' })).toBeNull();
  });
});
