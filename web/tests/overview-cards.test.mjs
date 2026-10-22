import assert from 'node:assert/strict';
import test from 'node:test';

// The cards are built in Russian: pin the locale before anything loads.
globalThis.localStorage = { getItem: () => 'ru' };

const { SUMMARIES } = await import('./fixtures/overview-summaries.mjs');
const cards = await import('../src/overviewCards.js');

const reportUrl = (id) => `/reports/${id}?stub`;

test('metricCard picks label and hint by name and a delta by what it is given', () => {
  const withChange = cards.metricCard('TotalRevenue', { value: '1 ₽', change: 5 });
  assert.equal(withChange.label, 'Общая выручка');
  assert.equal(withChange.deltaValue, 5);
  assert.match(withChange.delta, /\+5%/);

  const withNote = cards.metricCard('GoodsRevenueShare', { value: '3%', note: 'of total' });
  assert.equal(withNote.delta, 'of total');
  assert.equal(withNote.deltaValue, null);

  const bare = cards.metricCard('BranchShiftClients', { value: '7' });
  assert.equal('delta' in bare, false);
  assert.equal('present' in bare, false);

  const linked = cards.metricCard('NewClients', { value: '1', change: null, href: '/r', action: 'Open' });
  assert.equal(linked.href, '/r');
  assert.equal(linked.action, 'Open');
  assert.equal('delta' in linked, false);
});

test('a missing percent is "was 0" for a non-zero value, nothing for a zero one, and a real percent stays a percent', () => {
  const delta = (options) => cards.metricCard('TotalRevenue', { value: 'x', ...options });

  assert.equal(delta({ change: null, current: 150 }).delta, 'было 0');
  assert.equal(delta({ change: null, current: 150 }).deltaValue, null);
  assert.equal('delta' in delta({ change: null, current: 0 }), false);
  assert.equal('delta' in delta({ change: null, current: null }), false);
  assert.equal('delta' in delta({ change: undefined, current: undefined }), false);
  assert.match(delta({ change: 12.5, current: 150 }).delta, /\+12,5%/);
  assert.match(delta({ change: 0, current: 150 }).delta, /^0%/);

  // A summary that carries the previous value tells a zero base from an unknown one.
  assert.equal(delta({ change: null, current: 4, previous: 0 }).delta, 'было 0');
  assert.equal('delta' in delta({ change: null, current: 4, previous: null }), false);
  assert.equal('delta' in delta({ change: null, current: 4, previous: 3 }), false);
});

test("an administrator's cards read the previous value from his block", () => {
  const summary = {
    ...SUMMARIES.adminSchedule,
    administrator_personal: {
      staff_id: 1,
      company_id: 1,
      opz_manual_qty: 0,
      metrics: {
        cosmo_sum: { value: 5000, previous_value: 0, change_pct: null },
        clients: { value: 0, previous_value: 0, change_pct: null },
        opz_qty: { value: 3, previous_value: null, change_pct: null },
        opz_pct: { value: 10, previous_value: 5, change_pct: 100 },
      },
    },
  };
  const byLabel = Object.fromEntries(cards.administratorCards(summary).cards.map((card) => [card.label, card]));
  assert.equal(byLabel['Космо, сумма'].delta, 'было 0');
  assert.equal('delta' in byLabel['Закреплённые визиты'], false);
  assert.equal('delta' in byLabel['Количество ОПЗ'], false);
  assert.match(byLabel['Доля ОПЗ от визитов'].delta, /\+100%/);
});

test('the OPZ split cards carry the count line and the other cards do not', () => {
  const visit = cards.visitCards(SUMMARIES.full);
  const byLabel = Object.fromEntries(visit.map((card) => [card.label, card]));
  assert.equal(byLabel['Доля ОПЗ после первого визита'].count, '6 шт из 125 визитов');
  assert.equal(byLabel['Доля ОПЗ после повторного визита'].count, '49 шт из 717 визитов');
  assert.equal(visit.filter((card) => 'count' in card).length, 2);
  assert.equal('count' in byLabel['Количество ОПЗ'], false);
});

test('the count line follows the Russian number agreement and is left out without counts', () => {
  const withVisits = (visits) => cards.visitCards({
    visit_metrics: { opz_first_visit_qty: 1, first_visits: visits, opz_repeat_visit_qty: 0, repeat_visits: 0 },
  })[2].count;
  assert.equal(withVisits(21), '1 шт из 21 визита');
  assert.equal(withVisits(22), '1 шт из 22 визитов');
  assert.equal(withVisits(11), '1 шт из 11 визитов');
  assert.equal(withVisits(0), '1 шт из 0 визитов');
  assert.equal('count' in cards.visitCards({ visit_metrics: {} })[2], false);
});

test('the visit row is the four OPZ cards for everyone; extra services per visit lives in the services section only', () => {
  const labels = (summary) => cards.visitCards(summary).map((card) => card.label);
  assert.deepEqual(labels(SUMMARIES.full), labels(SUMMARIES.adminSchedule));
  assert.equal(labels(SUMMARIES.full).length, 4);
  assert.equal(labels(SUMMARIES.full).some((label) => label.includes('доп. услуг')), false);
  const services = cards.servicesCards(SUMMARIES.full).cards.map((card) => card.label);
  assert.equal(services.filter((label) => label === 'Доп. услуги от посещений').length, 1);
});

const labelsOf = (list) => list.map((card) => card.label);

test("an administrator's cards follow his Plan/fact row and carry the manual top-up under the OPZ total", () => {
  const { cards: administrator } = cards.administratorCards(SUMMARIES.adminSchedule);
  // No revenue / average check: an administrator's money metric is cosmetics.
  const labels = labelsOf(administrator);
  for (const money of ['Выручка администратора', 'Средний чек администратора']) assert.equal(labels.includes(money), false, money);
  const byLabel = Object.fromEntries(administrator.map((card) => [card.label, card]));
  assert.equal(byLabel['Количество ОПЗ'].value, '8');
  assert.equal(byLabel['Количество ОПЗ'].count, 'в т.ч. доп. ОПЗ: 1');
  assert.equal(byLabel['Доля ОПЗ от визитов'].value, '24,24%');
  assert.equal(byLabel['Закреплённые визиты'].deltaValue, -45.9);
  assert.equal(byLabel['Доп. услуги, шт'].value, '16');
  assert.equal(byLabel['% доп. услуг'].value, '37,21%');
  // The split keeps its own counts line; only the total carries the top-up.
  assert.equal(byLabel['Доля ОПЗ после первого визита'].count, '6 шт из 125 визитов');
});

test('no manual top-up, no note under the OPZ total', () => {
  const summary = {
    ...SUMMARIES.adminSchedule,
    administrator_personal: { ...SUMMARIES.adminSchedule.administrator_personal, opz_manual_qty: 0 },
  };
  const total = cards.administratorCards(summary).cards.find((card) => card.label === 'Количество ОПЗ');
  assert.equal('count' in total, false);
});

test('money the role may not see is absent from the block and from the cards', () => {
  const { cards: administrator } = cards.administratorCards(SUMMARIES.adminScheduleFinancialsHidden);
  const labels = labelsOf(administrator);
  for (const money of ['Выручка администратора', 'Средний чек администратора', 'Космо, сумма']) {
    assert.equal(labels.includes(money), false, money);
  }
  assert.ok(labels.includes('Закреплённые визиты'));
});

test('a summary without the administrator block has no administrator cards', () => {
  assert.deepEqual(cards.administratorCards(SUMMARIES.full).cards, []);
});

test('the local-estimate notice is for YClients not answering, not for every local count', () => {
  const notice = (breakdown) => cards.appointmentsCards({ appointments_breakdown: breakdown }).notice;
  const local = { source_status: 'local', total: 3, cancelled: 1, completed: 1, incomplete: 1 };
  assert.match(notice({ ...local, local_reason: 'upstream_error' }), /Точные метрики YClients временно недоступны/);
  assert.equal(notice({ ...local, local_reason: 'period_includes_today' }), '');
  assert.equal(notice({ ...local, local_reason: 'reporting_window' }), '');
  assert.equal(notice({ ...local, local_reason: 'staff_group' }), '');
  // An older API that does not say why stays quiet rather than crying wolf.
  assert.equal(notice(local), '');
  assert.match(notice({ source_status: 'unavailable' }), /временно недоступны/);
  assert.equal(notice({ source_status: 'ready' }), '');
});

test('client cards carry a report link only when the report url builder gives one', () => {
  const linked = (list) => list.filter((card) => card.href).map((card) => card.label);
  assert.deepEqual(linked(cards.clientsCards(SUMMARIES.full, () => null)), []);
  const withLinks = cards.clientsCards(SUMMARIES.full, reportUrl);
  assert.equal(linked(withLinks).length, 4);
  assert.ok(withLinks.filter((card) => card.href).every((card) => card.href === '/reports/new_vs_returning_cross?stub'));
});

test('a percent missing because the previous window has no data is not reported as "was 0"', () => {
  const visitMetrics = {
    ...SUMMARIES.full.visit_metrics,
    opz_first_visit_pct: 4,
    opz_first_visit_pct_change_pct: null,
    opz_first_visit_pct_previous: null,
    opz_repeat_visit_pct: 7,
    opz_repeat_visit_pct_change_pct: null,
    opz_repeat_visit_pct_previous: 0,
  };
  const byLabel = Object.fromEntries(cards.visitCards({ ...SUMMARIES.full, visit_metrics: visitMetrics })
    .map((card) => [card.label, card]));
  assert.equal('delta' in byLabel['Доля ОПЗ после первого визита'], false);
  assert.equal(byLabel['Доля ОПЗ после повторного визита'].delta, 'было 0');
});

test('the shift block keeps only the branch visits and clients: the extra services are the administrator\'s own', () => {
  assert.deepEqual(labelsOf(cards.staffBranchCards(SUMMARIES.adminSchedule)), [
    'Посещения филиала за смены',
    'Клиенты филиала за смены',
  ]);
});

test('without the summary blocks the cards are left out instead of failing', () => {
  assert.deepEqual(cards.kpiCards(SUMMARIES.noBlocks), []);
  assert.deepEqual(cards.revenueCards(SUMMARIES.noBlocks), []);
  assert.doesNotThrow(() => cards.servicesCards(SUMMARIES.noBlocks));
  assert.doesNotThrow(() => cards.clientsCards(SUMMARIES.noBlocks, reportUrl));
});

test('a partial average check is named differently from a complete one', () => {
  const average = (summary) => cards.kpiCards(summary).map((card) => card.label);
  assert.notDeepEqual(average(SUMMARIES.partialAverageCheck), average(SUMMARIES.full));
  assert.equal(average(SUMMARIES.partialAverageCheck).length, average(SUMMARIES.full).length);
});

test('an appointments source that is not available shows "no data" instead of numbers', () => {
  const { cards: appointments } = cards.appointmentsCards(SUMMARIES.adminScheduleFinancialsHidden);
  assert.equal(appointments.length, 4);
  for (const card of appointments) assert.equal(card.value, 'Нет данных');
});
