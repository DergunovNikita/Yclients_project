import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// The golden HTML was captured from the card code that lived in main.js before it moved into
// overviewCards.js (Russian locale, the fixture summaries). Pin the locale before anything loads.
globalThis.localStorage = { getItem: () => 'ru' };

const { SUMMARIES } = await import('./fixtures/overview-summaries.mjs');
const cards = await import('../src/overviewCards.js');
const golden = JSON.parse(readFileSync(new URL('./fixtures/overview-cards.golden.json', import.meta.url), 'utf8'));

const reportUrl = (id) => `/reports/${id}?stub`;

function renderAll(summary) {
  const entry = {};
  entry.kpi = cards.cardsHtml(cards.kpiCards(summary));
  entry.staffBranchHidden = !cards.usesAdministratorSchedule(summary);
  entry.staffBranch = entry.staffBranchHidden ? '' : cards.cardsHtml(cards.staffBranchCards(summary));
  entry.revenue = cards.cardsHtml(cards.revenueCards(summary));
  const services = cards.servicesCards(summary);
  entry.services = cards.cardsHtml(services.cards);
  entry.servicesColumns = String(services.columnCount);
  entry.visit = cards.cardsHtml(cards.visitCards(summary));
  entry.clients = cards.cardsHtml(cards.clientsCards(summary, reportUrl));
  const appointments = cards.appointmentsCards(summary);
  entry.appointments = cards.cardsHtml(appointments.cards);
  entry.appointmentsWarning = appointments.notice;
  return entry;
}

// Hint ids count up across renders, so the fixtures must render in the order the golden file was captured in.
const rendered = Object.fromEntries(Object.entries(SUMMARIES).map(([name, summary]) => [name, renderAll(summary)]));

for (const name of Object.keys(SUMMARIES)) {
  for (const section of Object.keys(golden[name])) {
    test(`overview cards: ${name} / ${section} render byte-for-byte as before`, () => {
      assert.equal(rendered[name][section], golden[name][section]);
    });
  }
}

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

test('every card name the builders use has a label and a hint in every locale', async () => {
  const locales = await Promise.all(['ru', 'en', 'it'].map(
    async (code) => JSON.parse(readFileSync(new URL(`../locales/${code}.json`, import.meta.url), 'utf8')),
  ));
  const source = readFileSync(new URL('../src/overviewCards.js', import.meta.url), 'utf8');
  const names = new Set([...source.matchAll(/metricCard\(\s*(?:[^'(]*\?\s*)?'(\w+)'/g)].map((match) => match[1]));
  [...source.matchAll(/'(\w+Partial)'/g)].forEach((match) => names.add(match[1]));
  [...source.matchAll(/share\('(\w+)'/g)].forEach((match) => names.add(match[1]));
  assert.ok(names.size > 25, `expected the card names to be found, got ${names.size}`);
  for (const locale of locales) {
    for (const name of names) {
      assert.equal(typeof locale.dash[`card${name}`], 'string', `dash.card${name}`);
      assert.equal(typeof locale.dash[`hint${name}`], 'string', `dash.hint${name}`);
    }
  }
});

test('the OPZ split cards carry the count line between value and delta, and the other cards do not', () => {
  const visit = cards.visitCards(SUMMARIES.full);
  const byLabel = Object.fromEntries(visit.map((card) => [card.label, card]));
  assert.equal(byLabel['Доля ОПЗ после первого визита'].count, '6 шт из 125 визитов');
  assert.equal(byLabel['Доля ОПЗ после повторного визита'].count, '49 шт из 717 визитов');
  assert.equal(visit.filter((card) => 'count' in card).length, 2);

  const html = cards.cardsHtml([byLabel['Доля ОПЗ после первого визита']]);
  assert.ok(html.indexOf('class="value"') < html.indexOf('class="card__count"'));
  assert.ok(html.indexOf('class="card__count"') < html.indexOf('class="delta'));
  assert.equal(cards.cardsHtml([byLabel['Количество ОПЗ']]).includes('card__count'), false);
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
  const { cards: administrator, columnCount } = cards.administratorCards(SUMMARIES.adminSchedule);
  // No revenue / average check: an administrator's money metric is cosmetics.
  assert.deepEqual(labelsOf(administrator), [
    'Закреплённые визиты',
    'Космо, шт',
    'Космо, сумма',
    'Количество ОПЗ',
    'Доля ОПЗ от визитов',
    'Доля ОПЗ после первого визита',
    'Доля ОПЗ после повторного визита',
    'Доп. услуги, шт',
    '% доп. услуг',
    'Отзывы',
  ]);
  assert.equal(columnCount, 5);
  const byLabel = Object.fromEntries(administrator.map((card) => [card.label, card]));
  assert.equal(byLabel['Количество ОПЗ'].value, '8');
  assert.equal(byLabel['Количество ОПЗ'].count, 'в т.ч. доп. ОПЗ: 1');
  assert.equal(byLabel['Доля ОПЗ от визитов'].value, '24,24%');
  assert.equal(byLabel['Закреплённые визиты'].deltaValue, -45.9);
  assert.equal(byLabel['Доп. услуги, шт'].value, '16');
  assert.equal(byLabel['% доп. услуг'].value, '37,21%');
  // The split keeps its own counts line; only the total carries the top-up.
  assert.equal(byLabel['Доля ОПЗ после первого визита'].count, '6 шт из 125 визитов');
  assert.equal(administrator.filter((card) => card.count).length, 3);
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
  const { cards: administrator, columnCount } = cards.administratorCards(SUMMARIES.adminScheduleFinancialsHidden);
  const labels = labelsOf(administrator);
  for (const money of ['Выручка администратора', 'Средний чек администратора', 'Космо, сумма']) {
    assert.equal(labels.includes(money), false, money);
  }
  assert.equal(labels.length, 9);
  assert.equal(columnCount, 5);
  assert.ok(labels.includes('Закреплённые визиты'));
});

test('a summary without the administrator block has no administrator cards', () => {
  assert.deepEqual(cards.administratorCards(SUMMARIES.full), { cards: [], columnCount: 4 });
});

test('every administrator card has a label and a hint in every locale', () => {
  for (const code of ['ru', 'en', 'it']) {
    const dash = JSON.parse(readFileSync(new URL(`../locales/${code}.json`, import.meta.url), 'utf8')).dash;
    for (const name of ['Clients', 'CosmoQty', 'CosmoSum', 'Reviews']) {
      assert.equal(typeof dash[`cardAdmin${name}`], 'string', `${code}: cardAdmin${name}`);
      assert.equal(typeof dash[`hintAdmin${name}`], 'string', `${code}: hintAdmin${name}`);
    }
  }
});

test('the local-estimate notice is for YClients not answering, not for every local count', () => {
  const notice = (breakdown) => cards.appointmentsCards({ appointments_breakdown: breakdown }).notice;
  const local = { source_status: 'local', total: 3, cancelled: 1, completed: 1, incomplete: 1 };
  assert.match(notice({ ...local, local_reason: 'upstream_error' }), /Точные метрики YClients временно недоступны/);
  assert.equal(notice({ ...local, local_reason: 'period_includes_today' }), '');
  assert.equal(notice({ ...local, local_reason: 'reporting_window' }), '');
  // An older API that does not say why stays quiet rather than crying wolf.
  assert.equal(notice(local), '');
  assert.match(notice({ source_status: 'unavailable' }), /временно недоступны/);
  assert.equal(notice({ source_status: 'ready' }), '');
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
