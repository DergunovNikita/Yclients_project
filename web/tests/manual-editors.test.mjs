import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// The golden trace was recorded from the editor code that lived in main.js before it became
// manualEntryEditor.js / manualEditors.js, driven through the same scripted conversations.
globalThis.localStorage = { getItem: () => 'ru' };

const { createManualEditors } = await import('../src/manualEditors.js');
const { createUnsavedChangesGuard } = await import('../src/unsavedChanges.js');
const { SCENARIOS, runScenarios } = await import('./helpers/manual-editor-scenarios.mjs');
const { makeEnv } = await import('./helpers/manual-editor-env.mjs');

const golden = JSON.parse(readFileSync(new URL('./fixtures/manual-editors.golden.json', import.meta.url), 'utf8'));

// What main.js does around the editors: the guard asks the active view's editor.
export function createImpl(env, hostOverrides = {}) {
  let activeView = 'overview';
  let editors;
  const guard = createUnsavedChangesGuard({
    isDirty: () => Boolean(editors.byView[activeView]?.dirty),
    isBlocked: () => Boolean(editors.byView[activeView]?.saving),
    confirmLeave: env.host.confirmLeave,
    onDiscard: () => editors.byView[activeView]?.discard(),
  });
  editors = createManualEditors({
    els: env.els,
    filterEls: env.filterEls,
    host: { ...env.host, ...hostOverrides, guard, filterDropdowns: env.customFilterDropdowns },
  });
  env.probe = () => ({
    review: { dirty: editors.review.dirty, saving: editors.review.saving },
    opz: { dirty: editors.opz.dirty, saving: editors.opz.saving },
    yandex: { dirty: editors.yandex.dirty, saving: editors.yandex.saving },
  });
  return {
    editors,
    setActiveView: (view) => { activeView = view; },
    discard: () => editors.byView[activeView]?.discard(),
  };
}

const traces = await runScenarios((env) => createImpl(env));

test('the golden trace covers every scenario and nothing else', () => {
  assert.deepEqual(Object.keys(traces), Object.keys(golden));
  assert.deepEqual(Object.keys(SCENARIOS), Object.keys(golden));
});

for (const name of Object.keys(golden)) {
  test(`manual editors behave exactly as before: ${name}`, () => {
    // JSON round trip so `undefined` and key order are judged the way the golden file was written.
    assert.deepEqual(JSON.parse(JSON.stringify(traces[name])), golden[name]);
    assert.equal(JSON.stringify(traces[name]), JSON.stringify(golden[name]));
  });
}

test('no scenario hit an unscripted request or threw', () => {
  const stray = Object.entries(traces).filter(([, log]) => JSON.stringify(log).match(/SCENARIO THREW|no scripted response/));
  assert.deepEqual(stray.map(([name]) => name), []);
});

test('a month change rebuilds the pickers only after the unsaved-changes dialog is answered', async () => {
  const env = makeEnv();
  const refilters = [];
  const impl = createImpl(env, {
    refilterForPeriod: async (filter) => {
      env.log.push(['refilterForPeriod', filter.month.id]);
      refilters.push(filter.month.id);
    },
  });
  impl.setActiveView('reviewFacts');
  env.script('GET /dashboard/plan/reviews_fact', { ok: { data: { rows: [{ company_id: 1, staff_id: 10, staff_name: 'Ann', value: 1 }] } } });
  await env.filterEls.reviewFacts.load.dispatch('click');
  env.els.reviewFactEditor.parsed.inputs[0].value = '2';
  await env.els.reviewFactEditor.dispatch('input', { target: env.els.reviewFactEditor.parsed.inputs[0] });

  env.confirmAnswer = false;
  env.filterEls.reviewFacts.month.value = '2026-08';
  await env.filterEls.reviewFacts.month.dispatch('change');
  assert.deepEqual(refilters, [], 'declined: nothing was rebuilt for a month the user did not keep');
  assert.equal(env.filterEls.reviewFacts.month.value, '2026-09');

  env.confirmAnswer = true;
  env.script('GET /dashboard/plan/reviews_fact', { ok: { data: { rows: [] } } });
  env.filterEls.reviewFacts.month.value = '2026-08';
  await env.filterEls.reviewFacts.month.dispatch('change');
  assert.deepEqual(refilters, ['review-month']);
  const order = env.log.map((entry) => entry[0]);
  assert.ok(order.lastIndexOf('refilterForPeriod') < order.lastIndexOf('fetchJson'), 'the lists are rebuilt before the load');
});

test('a month change whose refilter was superseded does not load', async () => {
  const env = makeEnv();
  const impl = createImpl(env, { refilterForPeriod: async () => false });
  impl.setActiveView('opzFacts');
  env.filterEls.opzFacts.month.value = '2026-08';
  await env.filterEls.opzFacts.month.dispatch('change');
  assert.equal(env.log.some((entry) => entry[0] === 'fetchJson'), false);
});
