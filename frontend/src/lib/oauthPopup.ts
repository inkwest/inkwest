import { centeredPopupFeatures } from '@/lib/popupCenter';

/**
 * Popup-blocker-safe OAuth consent popup, shared by the MCP and connector flows.
 *
 * Opens a blank window SYNCHRONOUSLY — this MUST be called straight from a click handler;
 * opening it after an await trips popup blockers — then navigates it to the authorization URL
 * resolved by `resolveUrl` (typically a backend round-trip). On failure the blank popup is
 * closed and `onError` is invoked with the thrown value. Returns the window so the caller can
 * poll it until it closes. Centred on the browser window (multi-monitor aware).
 */
export function openAuthPopup(
  name: string,
  size: { width: number; height: number },
  resolveUrl: () => Promise<string>,
  onError?: (error: unknown) => void,
): Window | null {
  const popup = window.open('about:blank', name, centeredPopupFeatures(size.width, size.height));
  void (async () => {
    try {
      const url = await resolveUrl();
      if (popup) popup.location.href = url;
    } catch (e) {
      popup?.close();
      onError?.(e);
    }
  })();
  return popup;
}
