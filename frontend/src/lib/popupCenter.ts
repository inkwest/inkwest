/**
 * `window.open` feature string that centres a popup on the CURRENT BROWSER WINDOW, not the
 * primary display.
 *
 * Centring off `window.screen` mis-places the popup on multi-monitor setups: it lands on the
 * primary screen even when the app window is on a secondary one. `screenLeft`/`screenTop` give
 * the browser window's position on the *virtual* desktop (which monitor it's actually on) and
 * `outerWidth`/`outerHeight` its full size — so the popup opens over the app regardless of
 * monitor layout. Deliberately NOT clamped to ≥0: a monitor left of / above the primary has
 * legitimately negative coordinates, and clamping would drag the popup back to the primary.
 */
export function centeredPopupFeatures(w: number, h: number): string {
  // screenLeft/Top: modern browsers; screenX/Y: older fallback. `??` keeps a valid 0.
  const baseLeft = window.screenLeft ?? window.screenX ?? 0;
  const baseTop = window.screenTop ?? window.screenY ?? 0;
  // outer* includes the browser chrome, so the popup centres on the whole window. Fall back to
  // inner*/screen where outer* is unavailable (0 in some embedded/test environments).
  const viewW = window.outerWidth || window.innerWidth || window.screen.width;
  const viewH = window.outerHeight || window.innerHeight || window.screen.height;
  // Centre within the window, but never past its top-left origin: if the popup is larger than
  // the window, (view - size)/2 goes negative and would straddle off the monitor edge — pin it
  // to the window origin instead (still on the right monitor, just top-left aligned).
  const left = Math.round(baseLeft + Math.max(0, (viewW - w) / 2));
  const top = Math.round(baseTop + Math.max(0, (viewH - h) / 2));
  return `width=${w},height=${h},left=${left},top=${top}`;
}
