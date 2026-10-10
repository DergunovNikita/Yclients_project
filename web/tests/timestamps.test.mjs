import assert from 'node:assert/strict';
import test from 'node:test';

import { formatBranchDateTime, parseServerInstant } from '../src/timestamps.js';

test('a naive backend timestamp is read as UTC, not as local time', () => {
  // The whole point: half past midnight UTC must stay half past midnight UTC, whatever
  // timezone the browser runs in. Read as local time it would land on the previous day.
  assert.equal(
    parseServerInstant('2026-09-15T00:30:00').toISOString(),
    '2026-09-15T00:30:00.000Z',
  );
  assert.equal(
    parseServerInstant('2026-09-15T00:30:00.123456').toISOString(),
    '2026-09-15T00:30:00.123Z',
  );
});

test('a timestamp that already carries a zone keeps it', () => {
  assert.equal(parseServerInstant('2026-09-15T00:30:00Z').toISOString(), '2026-09-15T00:30:00.000Z');
  assert.equal(
    parseServerInstant('2026-09-15T03:30:00+03:00').toISOString(),
    '2026-09-15T00:30:00.000Z',
  );
  assert.equal(
    parseServerInstant('2026-09-15T03:30:00+0300').toISOString(),
    '2026-09-15T00:30:00.000Z',
  );
});

test('nothing parseable yields null so callers can fall back', () => {
  assert.equal(parseServerInstant(''), null);
  assert.equal(parseServerInstant(null), null);
  assert.equal(parseServerInstant(undefined), null);
  assert.equal(parseServerInstant('not a date'), null);
});

test('formatBranchDateTime renders the instant in branch time, whatever the viewer zone is', () => {
  // 00:30 UTC on the 15th is 03:30 in Moscow, still the 15th; 22:30 UTC on the 14th is 01:30 on the 15th.
  const late = formatBranchDateTime('2026-09-14T22:30:00');
  assert.match(late, /15/);
  assert.match(late, /01:30:00/);
  assert.match(formatBranchDateTime('2026-09-15T00:30:00Z'), /03:30:00/);
});

test('formatBranchDateTime gives null for nothing parseable so callers pick the fallback', () => {
  assert.equal(formatBranchDateTime(null), null);
  assert.equal(formatBranchDateTime(''), null);
  assert.equal(formatBranchDateTime('not a date'), null);
});
