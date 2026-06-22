import { describe, it, expect, beforeEach } from 'vitest';
import { purgeLegacySessionToken } from './legacyAuthCleanup';

describe('purgeLegacySessionToken', () => {
  beforeEach(() => localStorage.clear());

  it('removes a stale session_token left from the pre-cookie scheme', () => {
    localStorage.setItem('session_token', 'stale-jwt');
    purgeLegacySessionToken();
    expect(localStorage.getItem('session_token')).toBeNull();
  });

  it('is a no-op when nothing is stored', () => {
    expect(() => purgeLegacySessionToken()).not.toThrow();
    expect(localStorage.getItem('session_token')).toBeNull();
  });

  it('leaves other localStorage keys untouched', () => {
    localStorage.setItem('session_token', 'stale-jwt');
    localStorage.setItem('keep-me', 'value');
    purgeLegacySessionToken();
    expect(localStorage.getItem('keep-me')).toBe('value');
  });
});
