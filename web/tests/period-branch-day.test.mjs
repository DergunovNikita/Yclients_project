import assert from 'node:assert/strict';
import test from 'node:test';

import {
  branchToday,
  defaultReportDates,
  inputDateValue,
  monthOfRange,
  monthRange,
  monthValue,
} from '../src/period.js';

const day = (date) => inputDateValue(date);

test('today is the branch day, not the day of the clock that asked', () => {
  // 21:00 UTC is midnight in Moscow (UTC+3): the branch is already on the next day.
  assert.equal(day(branchToday(new Date('2026-10-10T20:59:59Z'))), '2026-10-10');
  assert.equal(day(branchToday(new Date('2026-10-10T21:00:00Z'))), '2026-10-11');
  assert.equal(day(branchToday(new Date('2026-12-31T21:30:00Z'))), '2027-01-01');
});

test('the branch day does not depend on the viewer timezone', () => {
  const previous = process.env.TZ;
  try {
    for (const zone of ['Pacific/Kiritimati', 'America/Los_Angeles', 'Europe/Rome', 'UTC']) {
      process.env.TZ = zone;
      const today = branchToday(new Date('2026-10-10T22:30:00Z'));
      assert.equal(day(today), '2026-10-11', zone);
      assert.equal(today.getHours(), 0, zone);
    }
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
});

test('the defaults follow the day they are asked for, not a frozen one', () => {
  const lateOnTheLastDay = branchToday(new Date('2026-09-30T20:00:00Z'));
  const justAfterMidnight = branchToday(new Date('2026-09-30T21:00:00Z'));
  assert.deepEqual(defaultReportDates(lateOnTheLastDay), { start: '2026-09-01', end: '2026-09-30' });
  assert.deepEqual(defaultReportDates(justAfterMidnight), { start: '2026-10-01', end: '2026-10-01' });
  assert.equal(monthValue(lateOnTheLastDay), '2026-09');
  assert.equal(monthValue(justAfterMidnight), '2026-10');
});

test('the running month is cut at the branch day', () => {
  const today = branchToday(new Date('2026-10-10T21:30:00Z'));
  assert.deepEqual(monthRange('2026-10', today), { start: '2026-10-01', end: '2026-10-11' });
  assert.deepEqual(monthRange('2026-09', today), { start: '2026-09-01', end: '2026-09-30' });
  assert.equal(monthOfRange('2026-10-01', '2026-10-11', today), '2026-10');
});

test('without a clock argument the helpers read the branch day at call time', () => {
  const expectedEnd = day(branchToday());
  assert.equal(defaultReportDates().end, expectedEnd);
  const month = monthValue(branchToday());
  assert.equal(monthRange(month).end, expectedEnd);
});
