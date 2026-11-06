import assert from 'node:assert/strict';
import test from 'node:test';

import { jpegToPdf, MAX_CANVAS_PIXELS, snapshotFileName, snapshotPixelRatio } from '../src/snapshotFile.js';

const jpeg = Uint8Array.of(0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x0a, 0x0d, 0x00, 0xff, 0xd9, 0x80, 0x81);
const latin1 = (bytes) => Buffer.from(bytes).toString('latin1');

function indexOfBytes(haystack, needle, from = 0) {
  return Buffer.from(haystack).indexOf(Buffer.from(needle, 'latin1'), from);
}

test('every xref entry is 20 bytes and points at its object', () => {
  const pdf = jpegToPdf(jpeg, 800, 600);
  const text = latin1(pdf);
  const xrefAt = text.lastIndexOf('xref\n0 6\n');
  assert.ok(xrefAt > 0);
  const table = text.slice(xrefAt + 'xref\n0 6\n'.length);
  const entries = [...table.matchAll(/(\d{10}) (\d{5}) ([nf]) \n/g)];
  assert.equal(entries.length, 6);
  assert.ok(entries.every((entry) => entry[0].length === 20));
  assert.equal(entries[0][3], 'f');
  entries.slice(1).forEach((entry, index) => {
    const offset = Number(entry[1]);
    assert.ok(text.startsWith(`${index + 1} 0 obj\n`, offset), `object ${index + 1} at ${offset}`);
  });
});

test('startxref points at the xref table and the file ends with %%EOF', () => {
  const pdf = jpegToPdf(jpeg, 800, 600);
  const text = latin1(pdf);
  const offset = Number(/startxref\n(\d+)\n%%EOF\n$/.exec(text)[1]);
  assert.ok(text.startsWith('xref\n', offset));
  assert.match(text, /trailer\n<< \/Size 6 \/Root 1 0 R >>/);
  assert.ok(text.startsWith('%PDF-1.4\n%'));
});

test('page size is pixels times 0.75 and the content stream draws the image over it', () => {
  const text = latin1(jpegToPdf(jpeg, 800, 600));
  assert.match(text, /\/MediaBox \[0 0 600 450\]/);
  assert.match(text, /q 600 0 0 450 0 0 cm \/Im0 Do Q/);
  assert.match(text, /\/Width 800 \/Height 600 \/ColorSpace \/DeviceRGB \/BitsPerComponent 8 \/Filter \/DCTDecode/);
});

test('page numbers keep at most two decimals', () => {
  const text = latin1(jpegToPdf(jpeg, 801, 333));
  assert.match(text, /\/MediaBox \[0 0 600\.75 249\.75\]/);
  const numbers = /\/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]/.exec(text).slice(1);
  assert.ok(numbers.every((value) => !/\.\d{3,}/.test(value)));
});

test('a tall page is scaled down so the longer side stays within 14400 pt', () => {
  const text = latin1(jpegToPdf(jpeg, 1000, 40000));
  const [width, height] = /\/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]/.exec(text).slice(1).map(Number);
  assert.equal(height, 14400);
  assert.ok(Math.abs(width / height - 1000 / 40000) < 1e-3);
  assert.match(text, new RegExp(`q ${width} 0 0 14400 0 0 cm`));
});

test('image bytes are embedded verbatim and /Length matches', () => {
  const pdf = jpegToPdf(jpeg, 10, 10);
  const text = latin1(pdf);
  assert.match(text, new RegExp(`/Length ${jpeg.length} >>\\nstream\\n`));
  const start = indexOfBytes(pdf, 'stream\n') + 'stream\n'.length;
  assert.deepEqual([...pdf.slice(start, start + jpeg.length)], [...jpeg]);
  assert.equal(latin1(pdf.slice(start + jpeg.length, start + jpeg.length + 17)), '\nendstream\nendobj');
});

test('invalid input throws TypeError', () => {
  assert.throws(() => jpegToPdf([0xff, 0xd8], 10, 10), TypeError);
  assert.throws(() => jpegToPdf(Uint8Array.of(0x89, 0x50), 10, 10), TypeError);
  assert.throws(() => jpegToPdf(new Uint8Array(0), 10, 10), TypeError);
  assert.throws(() => jpegToPdf(jpeg, 0, 10), TypeError);
  assert.throws(() => jpegToPdf(jpeg, 10, -1), TypeError);
  assert.throws(() => jpegToPdf(jpeg, 10.5, 10), TypeError);
  assert.throws(() => jpegToPdf(jpeg, NaN, 10), TypeError);
});

test('file name joins parts, drops forbidden characters and keeps Cyrillic', () => {
  assert.equal(snapshotFileName(['План / факт', '  Сентябрь 2026 '], 'png'), 'План факт — Сентябрь 2026.png');
  assert.equal(snapshotFileName(['a:b*c?d"e<f>g|h\\i', 'x\ny\u0000z'], 'pdf'), 'a b c d e f g h i — x y z.pdf');
});

test('file name skips empty parts, falls back and trims dots and spaces', () => {
  assert.equal(snapshotFileName(['', '   ', undefined], 'png'), 'table.png');
  assert.equal(snapshotFileName(['///'], 'png'), 'table.png');
  assert.equal(snapshotFileName(['Отчёт...'], 'png'), 'Отчёт.png');
});

test('file name is cut to 80 characters without a trailing space or dot', () => {
  const name = snapshotFileName(['а'.repeat(79) + ' б'], 'png');
  assert.equal(name, `${'а'.repeat(79)}.png`);
  const long = snapshotFileName(['x'.repeat(200)], 'png');
  assert.equal(long, `${'x'.repeat(80)}.png`);
});

test('pixel ratio is capped at 2 and never lets the canvas exceed the pixel budget', () => {
  assert.equal(snapshotPixelRatio(400, 300), 2);
  for (const [width, height] of [[1200, 9000], [5000, 5000], [900, 30000], [333, 77777], [20000, 1000]]) {
    const ratio = snapshotPixelRatio(width, height);
    assert.ok(ratio > 0 && ratio <= 2);
    assert.ok(width * height * ratio * ratio <= MAX_CANVAS_PIXELS, `${width}x${height} -> ${ratio}`);
    assert.equal(ratio, Math.floor(ratio * 100) / 100);
  }
});

test('pixel ratio falls back to 1 for unusable sizes', () => {
  for (const [width, height] of [[0, 100], [100, -5], [NaN, 100], [100, undefined], [Infinity, 10]]) {
    assert.equal(snapshotPixelRatio(width, height), 1);
  }
});
