import assert from 'node:assert/strict';
import test from 'node:test';

import enLocale from '../locales/en.json' with { type: 'json' };
import itLocale from '../locales/it.json' with { type: 'json' };
import ruLocale from '../locales/ru.json' with { type: 'json' };
import {
  UNREADABLE_DATE,
  YANDEX_PAY_MAX,
  buildYandexPayItems,
  parseYandexPayDataThrough,
  parseYandexPayInput,
  syncYandexPayDate,
  yandexPayDateKey,
  yandexPayDraftKey,
  yandexPayMonthStart,
  yandexPayTotal,
} from '../src/yandexPayInput.js';

test('an empty cell is nothing entered, and zero is an entered zero', () => {
  assert.deepEqual(parseYandexPayInput(''), { value: null });
  assert.deepEqual(parseYandexPayInput('   '), { value: null });
  assert.deepEqual(parseYandexPayInput(null), { value: null });
  assert.deepEqual(parseYandexPayInput('0'), { value: 0 });
  assert.deepEqual(parseYandexPayInput('0,00'), { value: 0 });
});

test('a comma is a decimal point and spaces group digits', () => {
  assert.deepEqual(parseYandexPayInput('1250,5'), { value: 1250.5 });
  assert.deepEqual(parseYandexPayInput('1 250 000,25'), { value: 1250000.25 });
  assert.deepEqual(parseYandexPayInput('1 250'), { value: 1250 });
  assert.deepEqual(parseYandexPayInput(' 99.99 '), { value: 99.99 });
});

test('at most two decimals, trailing zeros do not count', () => {
  assert.deepEqual(parseYandexPayInput('10.50'), { value: 10.5 });
  assert.deepEqual(parseYandexPayInput('10.500'), { value: 10.5 });
  assert.deepEqual(parseYandexPayInput('10.005'), { error: 'precision' });
  assert.deepEqual(parseYandexPayInput('0,123'), { error: 'precision' });
});

test('the amount is bounded to 0..1e9', () => {
  assert.deepEqual(parseYandexPayInput(String(YANDEX_PAY_MAX)), { value: YANDEX_PAY_MAX });
  assert.deepEqual(parseYandexPayInput('1000000000.01'), { error: 'range' });
  assert.deepEqual(parseYandexPayInput('-1'), { error: 'range' });
  assert.deepEqual(parseYandexPayInput('-0'), { error: 'range' });
});

test('anything that is not a plain decimal is a format error', () => {
  for (const text of ['abc', '12x', '1e5', '1.2.3', '.5', '5.', '1,2,3', '+5', 'Infinity', 'NaN']) {
    assert.deepEqual(parseYandexPayInput(text), { error: 'format' }, text);
  }
});

test('a draft key ignores formatting but never hides a half-typed cell', () => {
  assert.equal(yandexPayDraftKey('100'), yandexPayDraftKey('100,00'));
  assert.equal(yandexPayDraftKey(''), '');
  assert.notEqual(yandexPayDraftKey('0'), yandexPayDraftKey(''));
  assert.notEqual(yandexPayDraftKey('12x'), yandexPayDraftKey(''));
  assert.notEqual(yandexPayDraftKey('12x'), yandexPayDraftKey('12y'));
});

const MONTH = '2026-10';
const ROWS = [
  {
    company_id: 1, company_title: 'Арбат', value: 1000.5, data_through: '2026-10-05',
    default_data_through: '2026-10-07', max_data_through: '2026-10-08', counted: true,
  },
  {
    company_id: 2, company_title: 'Тверская', value: null, data_through: null,
    default_data_through: '2026-10-07', max_data_through: '2026-10-08', counted: true,
  },
  {
    company_id: 3, company_title: 'ВДНХ', value: 70, data_through: '2026-10-08',
    default_data_through: '2026-10-07', max_data_through: '2026-10-08', counted: false,
  },
];

test('items carry the pair the last response held as previous_*', () => {
  const typed = new Map([['1', '2 000,25'], ['2', ''], ['3', '70']]);
  const dates = new Map([['1', '2026-10-06'], ['2', ''], ['3', '2026-10-08']]);
  assert.deepEqual(buildYandexPayItems(ROWS, typed, dates, MONTH), {
    items: [
      {
        company_id: 1, value: 2000.25, data_through: '2026-10-06',
        previous_value: 1000.5, previous_data_through: '2026-10-05',
      },
      {
        company_id: 2, value: null, data_through: null,
        previous_value: null, previous_data_through: null,
      },
      {
        company_id: 3, value: 70, data_through: '2026-10-08',
        previous_value: 70, previous_data_through: '2026-10-08',
      },
    ],
  });
});

test('an unchanged pair is still sent, with previous_* equal to the new pair', () => {
  const typed = new Map([['1', '1000,50'], ['2', ''], ['3', '70']]);
  const dates = new Map([['1', '2026-10-05'], ['2', ''], ['3', '2026-10-08']]);
  const { items } = buildYandexPayItems(ROWS, typed, dates, MONTH);
  assert.equal(items.length, ROWS.length);
  for (const item of items.filter((entry) => entry.value !== null)) {
    assert.equal(item.value, item.previous_value);
    assert.equal(item.data_through, item.previous_data_through);
  }
});

test('previous_* come from the last response, not from the cells', () => {
  const rows = [{ ...ROWS[0], value: 10, data_through: '2026-10-01' }];
  const items = buildYandexPayItems(rows, new Map([['1', '99']]), new Map([['1', '2026-10-04']]), MONTH).items;
  assert.equal(items[0].previous_value, 10);
  assert.equal(items[0].previous_data_through, '2026-10-01');
});

test('every row is sent, edited or not, and a stored zero stays zero', () => {
  const rows = [{ company_id: 5, value: 0, data_through: '2026-10-02', max_data_through: '2026-10-08' }];
  const dates = new Map([['5', '2026-10-02']]);
  assert.deepEqual(buildYandexPayItems(rows, new Map([['5', '0']]), dates, MONTH).items, [
    { company_id: 5, value: 0, data_through: '2026-10-02', previous_value: 0, previous_data_through: '2026-10-02' },
  ]);
  // Clearing a stored value is a change to null, not a skipped row, and it drops the date.
  assert.deepEqual(buildYandexPayItems(rows, new Map([['5', '']]), dates, MONTH).items, [
    { company_id: 5, value: null, data_through: null, previous_value: 0, previous_data_through: '2026-10-02' },
  ]);
});

test('an amount without a date is refused, not silently given the default', () => {
  // The server would substitute the month's default, which for a row with a stored date is a quiet edit of it.
  const typed = new Map([['1', '5']]);
  for (const dates of [new Map([['1', '']]), new Map([['1', '  ']]), new Map()]) {
    const result = buildYandexPayItems([ROWS[0]], typed, dates, MONTH);
    assert.equal(result.error, 'dateRequired');
    assert.equal(result.row.company_id, 1);
  }
  // No amount, no date needed.
  assert.equal(buildYandexPayItems([ROWS[1]], new Map([['2', '']]), new Map(), MONTH).items[0].data_through, null);
});

test('the first unreadable cell stops the save and names its row', () => {
  const typed = new Map([['1', '10'], ['2', '1.234'], ['3', 'abc']]);
  const result = buildYandexPayItems(ROWS, typed, new Map([['1', '2026-10-05']]), MONTH);
  assert.equal(result.error, 'precision');
  assert.equal(result.row.company_id, 2);
  assert.equal(result.items, undefined);
});

test('an invalid date is rejected with its own error and row', () => {
  const typed = new Map([['1', '10'], ['2', '5'], ['3', '']]);
  const bad = (date) => buildYandexPayItems(ROWS, typed, new Map([['1', '2026-10-05'], ['2', date]]), MONTH);
  assert.equal(bad('2026-10-31').error, 'dateRange'); // after max_data_through
  assert.equal(bad('2026-09-30').error, 'dateRange'); // before the 1st
  assert.equal(bad('2026-11-01').error, 'dateRange');
  assert.equal(bad('2026-02-30').error, 'dateFormat');
  assert.equal(bad('07.10.2026').error, 'dateFormat');
  assert.equal(bad(UNREADABLE_DATE).error, 'dateFormat');
  assert.equal(bad('2026-10-08').error, undefined);
  assert.equal(bad('2026-10-01').error, undefined);
  assert.equal(bad('x').row.company_id, 2);
});

test('the date of an emptied amount is not validated or sent', () => {
  const rows = [ROWS[0]];
  const result = buildYandexPayItems(rows, new Map([['1', '']]), new Map([['1', 'garbage']]), MONTH);
  assert.equal(result.items[0].data_through, null);
});

test('month start and date parsing', () => {
  assert.equal(yandexPayMonthStart('2026-10'), '2026-10-01');
  assert.equal(yandexPayMonthStart(''), '');
  assert.equal(yandexPayMonthStart('2026-13'), '');
  assert.deepEqual(parseYandexPayDataThrough('', ROWS[0], MONTH), { value: null });
  assert.deepEqual(parseYandexPayDataThrough('2026-10-03', ROWS[0], MONTH), { value: '2026-10-03' });
  assert.deepEqual(parseYandexPayDataThrough('2026-10-09', ROWS[0], MONTH), { error: 'range' });
});

test('typing an amount into an empty row proposes the default date', () => {
  assert.equal(syncYandexPayDate('100', '', '2026-10-07'), '2026-10-07');
  assert.equal(syncYandexPayDate('0', '', '2026-10-07'), '2026-10-07');
  assert.equal(syncYandexPayDate('100', '', undefined), '');
});

test('a date the user chose survives further typing', () => {
  assert.equal(syncYandexPayDate('1000', '2026-10-03', '2026-10-07'), '2026-10-03');
});

test('clearing the amount clears the date, a half-typed amount leaves it', () => {
  assert.equal(syncYandexPayDate('', '2026-10-03', '2026-10-07'), '');
  assert.equal(syncYandexPayDate('  ', '2026-10-03', '2026-10-07'), '');
  assert.equal(syncYandexPayDate('12x', '2026-10-03', '2026-10-07'), '2026-10-03');
  assert.equal(syncYandexPayDate('12x', '', '2026-10-07'), '');
});

test('a date change alone is a different draft', () => {
  assert.equal(yandexPayDateKey(' 2026-10-03 '), '2026-10-03');
  assert.notEqual(yandexPayDateKey('2026-10-03'), yandexPayDateKey('2026-10-04'));
  assert.equal(yandexPayDateKey(null), '');
});

test('the date messages exist in every locale', () => {
  for (const locale of [ruLocale, enLocale, itLocale]) {
    for (const key of ['yandexPayDataThrough', 'yandexPayInvalidDateFormat', 'yandexPayInvalidDateRange', 'yandexPayInvalidDateRequired']) {
      assert.ok(locale.dash[key], key);
    }
    assert.match(locale.dash.yandexPayInvalidDateRange, /\{branch\}/);
  }
});

test('the total counts only counted rows and only readable cells', () => {
  const typed = new Map([['1', '100,10'], ['2', '200.20'], ['3', '999']]);
  assert.equal(yandexPayTotal(ROWS, typed), 300.3);
  assert.equal(yandexPayTotal(ROWS, new Map([['1', '12x'], ['2', ''], ['3', '5']])), 0);
  // 0.1 + 0.2 in floats is 0.30000000000000004; amounts are summed in kopecks.
  assert.equal(yandexPayTotal(ROWS, new Map([['1', '0,1'], ['2', '0,2']])), 0.3);
});
