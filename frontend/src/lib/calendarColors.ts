import type { CalendarPalette } from '../theme';

export type EditorialPigmentKey =
  | 'rose'
  | 'ochre'
  | 'sand'
  | 'sage'
  | 'teal'
  | 'blue'
  | 'iris'
  | 'plum'
  | 'slate';

export const EDITORIAL_PIGMENT_DOT: Record<EditorialPigmentKey, string> = {
  rose: '#b86b68',
  ochre: '#b88252',
  sand: '#a68f4b',
  sage: '#5a8770',
  teal: '#4e828c',
  blue: '#57789c',
  iris: '#7e6e9e',
  plum: '#a36b84',
  slate: '#7c858c',
};

function parseHexRgb(hex: string | null | undefined): [number, number, number] | null {
  if (!hex) return null;
  const raw = hex.trim().replace(/^#/, '');
  if (/^[0-9a-f]{3}$/i.test(raw)) {
    return [
      parseInt(raw[0] + raw[0], 16),
      parseInt(raw[1] + raw[1], 16),
      parseInt(raw[2] + raw[2], 16),
    ];
  }
  if (/^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(raw)) {
    return [
      parseInt(raw.slice(0, 2), 16),
      parseInt(raw.slice(2, 4), 16),
      parseInt(raw.slice(4, 6), 16),
    ];
  }
  return null;
}

export function getEditorialPigmentKey(hex: string | null | undefined): EditorialPigmentKey {
  const rgb = parseHexRgb(hex);
  if (!rgb) return 'slate';
  const r = rgb[0] / 255;
  const g = rgb[1] / 255;
  const b = rgb[2] / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  if (delta === 0) return 'slate';

  const l = (max + min) / 2;
  const s = delta / (1 - Math.abs(2 * l - 1));
  if (s < 0.12) return 'slate';

  let h = 0;
  if (max === r) {
    h = ((g - b) / delta) % 6;
  } else if (max === g) {
    h = (b - r) / delta + 2;
  } else {
    h = (r - g) / delta + 4;
  }
  h = (h * 60 + 360) % 360;

  if (h < 18 || h >= 342) return 'rose';
  if (h < 40) return 'ochre';
  if (h < 74) return 'sand';
  if (h < 168) return 'sage';
  if (h < 198) return 'teal';
  if (h < 245) return 'blue';
  if (h < 302) return 'iris';
  return 'plum';
}

export function getDisplaySourceColor(hex: string | null | undefined, palette: CalendarPalette): string {
  if (palette === 'calm') {
    return EDITORIAL_PIGMENT_DOT[getEditorialPigmentKey(hex)];
  }
  return hex || '#6b7f89';
}
