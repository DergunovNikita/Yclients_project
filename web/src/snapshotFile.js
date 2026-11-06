// Pure helpers for table snapshots: canvas size cap, file names and a minimal one-page PDF
// around a JPEG. No DOM and no i18n, so node tests load it directly.

// iOS Safari refuses canvases above 16 777 216 pixels; stay a little under.
export const MAX_CANVAS_PIXELS = 16_000_000;

const MAX_PIXEL_RATIO = 2;
const MAX_FILE_NAME_LENGTH = 80;
// Viewers reject pages longer than 200 inches.
const MAX_PAGE_POINTS = 14400;
const POINTS_PER_PIXEL = 0.75;

export function snapshotPixelRatio(cssWidth, cssHeight) {
  const area = cssWidth * cssHeight;
  if (!(area > 0) || !Number.isFinite(area)) return 1;
  const ratio = Math.min(MAX_PIXEL_RATIO, Math.sqrt(MAX_CANVAS_PIXELS / area));
  // Rounding down keeps width * height * ratio² under the cap.
  return Math.floor(ratio * 100) / 100;
}

const FORBIDDEN_NAME_CHARS = new Set('/\\:*?"<>|');

// Character loop instead of a regex: tests/browser-auth-scan.mjs backtracks badly on a lone quote
// next to backslashes in a regex literal.
function replaceForbidden(value) {
  return Array.from(value, (char) => {
    const code = char.codePointAt(0);
    return FORBIDDEN_NAME_CHARS.has(char) || code < 32 || code === 127 ? ' ' : char;
  }).join('');
}

export function snapshotFileName(parts, ext) {
  const name = replaceForbidden(
    parts
      .filter((part) => typeof part === 'string' && part.trim())
      .map((part) => part.trim())
      .join(' — '),
  )
    .replace(/\s+/g, ' ')
    .trim();
  const cut = Array.from(name).slice(0, MAX_FILE_NAME_LENGTH).join('').replace(/[\s.]+$/, '');
  return `${cut || 'table'}.${ext}`;
}

const encoder = new TextEncoder();

function formatNumber(value) {
  return String(Number(value.toFixed(2)));
}

function isPositiveInteger(value) {
  return Number.isInteger(value) && value > 0;
}

export function jpegToPdf(jpegBytes, widthPx, heightPx) {
  if (!(jpegBytes instanceof Uint8Array) || jpegBytes[0] !== 0xff || jpegBytes[1] !== 0xd8) {
    throw new TypeError('jpegBytes must be a Uint8Array holding a JPEG');
  }
  if (!isPositiveInteger(widthPx) || !isPositiveInteger(heightPx)) {
    throw new TypeError('widthPx and heightPx must be positive integers');
  }

  const scale = Math.min(1, MAX_PAGE_POINTS / (Math.max(widthPx, heightPx) * POINTS_PER_PIXEL));
  const pageWidth = formatNumber(widthPx * POINTS_PER_PIXEL * scale);
  const pageHeight = formatNumber(heightPx * POINTS_PER_PIXEL * scale);
  const content = `q ${pageWidth} 0 0 ${pageHeight} 0 0 cm /Im0 Do Q`;

  const chunks = [];
  const offsets = [];
  let length = 0;
  const push = (chunk) => {
    chunks.push(chunk);
    length += chunk.length;
  };
  const text = (value) => push(encoder.encode(value));
  // Offsets are byte offsets: the header carries binary bytes, so never measure strings.
  const object = (number, body) => {
    offsets[number] = length;
    text(`${number} 0 obj\n${body}\nendobj\n`);
  };

  // The high-bit comment marks the file as binary for transfer tools.
  push(Uint8Array.of(...encoder.encode('%PDF-1.4\n%'), 0xe2, 0xe3, 0xcf, 0xd3, 0x0a));
  object(1, '<< /Type /Catalog /Pages 2 0 R >>');
  object(2, '<< /Type /Pages /Kids [3 0 R] /Count 1 >>');
  object(
    3,
    `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageWidth} ${pageHeight}] ` +
      '/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>',
  );
  offsets[4] = length;
  text(
    `4 0 obj\n<< /Type /XObject /Subtype /Image /Width ${widthPx} /Height ${heightPx} ` +
      '/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode ' +
      `/Length ${jpegBytes.length} >>\nstream\n`,
  );
  push(jpegBytes);
  text('\nendstream\nendobj\n');
  object(5, `<< /Length ${content.length} >>\nstream\n${content}\nendstream`);

  const xrefOffset = length;
  const entries = offsets.slice(1).map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`);
  text(
    `xref\n0 ${offsets.length}\n0000000000 65535 f \n${entries.join('')}` +
      `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`,
  );

  const pdf = new Uint8Array(length);
  let position = 0;
  for (const chunk of chunks) {
    pdf.set(chunk, position);
    position += chunk.length;
  }
  return pdf;
}
