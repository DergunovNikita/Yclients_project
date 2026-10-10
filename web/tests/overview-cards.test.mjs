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
  assert.equal(linked.deltaValue, null);
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
