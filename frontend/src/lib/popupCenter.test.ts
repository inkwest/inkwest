import { describe, it, expect, afterEach, vi } from 'vitest';
import { centeredPopupFeatures } from './popupCenter';

describe('centeredPopupFeatures', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('centres on the browser window, not the primary screen', () => {
    // Browser sits on a secondary monitor offset +2560px; window is 1600×1000.
    vi.stubGlobal('screenLeft', 2560);
    vi.stubGlobal('screenTop', 100);
    vi.stubGlobal('outerWidth', 1600);
    vi.stubGlobal('outerHeight', 1000);
    // 1600-600=1000→/2=500→+2560=3060; 1000-720=280→/2=140→+100=240.
    expect(centeredPopupFeatures(600, 720)).toBe('width=600,height=720,left=3060,top=240');
  });

  it('honours a negative offset (monitor left of / above the primary)', () => {
    vi.stubGlobal('screenLeft', -1920);
    vi.stubGlobal('screenTop', -200);
    vi.stubGlobal('outerWidth', 1920);
    vi.stubGlobal('outerHeight', 1080);
    // 1920-600=1320→660→-1920=-1260; 1080-720=360→180→-200=-20. Not clamped to 0.
    expect(centeredPopupFeatures(600, 720)).toBe('width=600,height=720,left=-1260,top=-20');
  });

  it('pins to the window origin when the popup is larger than the window', () => {
    // 800px-wide window, 960px popup → (800-960)/2 is negative; floored to 0 at the origin.
    vi.stubGlobal('screenLeft', 2560);
    vi.stubGlobal('screenTop', 100);
    vi.stubGlobal('outerWidth', 800);
    vi.stubGlobal('outerHeight', 600);
    // left = 2560 + max(0, -80) = 2560; top = 100 + max(0, -70) = 100.
    expect(centeredPopupFeatures(960, 740)).toBe('width=960,height=740,left=2560,top=100');
  });

  it('falls back to screen dims when outer* is 0', () => {
    vi.stubGlobal('screenLeft', 0);
    vi.stubGlobal('screenTop', 0);
    vi.stubGlobal('outerWidth', 0);
    vi.stubGlobal('outerHeight', 0);
    vi.stubGlobal('innerWidth', 0);
    vi.stubGlobal('innerHeight', 0);
    vi.stubGlobal('screen', { width: 1024, height: 768 });
    // width fallback outer→inner→screen: 1024-600=424→212; 768-720=48→24.
    expect(centeredPopupFeatures(600, 720)).toBe('width=600,height=720,left=212,top=24');
  });
});
