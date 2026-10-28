import type { AnnouncementLevel } from '@/types/api';

// Tailwind blue-600, green-700, amber-500, red-600 as hex so opacity can go through rgba().
// green-700, not 600: white on green-600 is 3.3:1, below AA.
export const LEVEL_BG: Record<AnnouncementLevel, string> = {
  info: '#2563eb',
  success: '#15803d',
  warning: '#f59e0b',
  danger: '#dc2626',
};

// A translucent bar is seen over ShellLayout's bg-gray-900, so text is picked on the blend.
const SHELL_BG = [0x11, 0x18, 0x27];

function rgbOf(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// Black or white, whichever contrasts more (WCAG) with the color as drawn: the max of the two
// is always >= 4.58:1, and at 100% it gives each level its own text (white, black on amber).
export function textColorFor(hex: string, percent: number): 'black' | 'white' {
  const alpha = percent / 100;
  const [r, g, b] = rgbOf(hex).map((c, i) => {
    const s = (c * alpha + SHELL_BG[i] * (1 - alpha)) / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) ? 'black' : 'white';
}

export function withOpacity(hex: string, percent: number): string {
  const rgb = rgbOf(hex).join(', ');
  return percent >= 100 ? `rgb(${rgb})` : `rgba(${rgb}, ${percent / 100})`;
}
