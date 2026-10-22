import assert from 'node:assert/strict';
import test, { afterEach } from 'node:test';

globalThis.localStorage = { getItem: () => 'ru' };

const { createManualEditors } = await import('../src/manualEditors.js');
const { createUnsavedChangesGuard } = await import('../src/unsavedChanges.js');
const { OPZ_ROWS, REVIEW_ROWS, YANDEX_FUTURE, YANDEX_ROWS, makeEnv } = await import('./helpers/manual-editor-env.mjs');

// What main.js does around the editors: the guard asks the active view's editor.
function createImpl(env, hostOverrides = {}) {
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

const ok = (value) => ({ ok: structuredClone(value) });
const fail = (message, status, apiStatus) => ({ error: { message, status, apiStatus } });
const tick = () => new Promise((resolve) => { setImmediate(resolve); });
const withRows = (source, edit) => {
  const copy = structuredClone(source);
  edit(copy.data.rows);
  return copy;
};

const REVIEW = {
  view: 'reviewFacts',
  get: 'GET /dashboard/plan/reviews_fact',
  post: 'POST /dashboard/plan/reviews_fact',
  path: '/dashboard/plan/reviews_fact',
  rows: REVIEW_ROWS,
  filter: (env) => env.filterEls.reviewFacts,
  container: (env) => env.els.reviewFactEditor,
  save: (env) => env.els.reviewFactSave,
  state: (env) => env.probe().review,
};
const OPZ = {
  view: 'opzFacts',
  get: 'GET /dashboard/plan/opz_fact',
  post: 'POST /dashboard/plan/opz_fact',
  path: '/dashboard/plan/opz_fact',
  rows: OPZ_ROWS,
  filter: (env) => env.filterEls.opzFacts,
  container: (env) => env.els.opzFactEditor,
  save: (env) => env.els.opzFactSave,
  state: (env) => env.probe().opz,
};
const YANDEX_PATH = '/dashboard/payments/yandex_pay';
const YANDEX_GET = `GET ${YANDEX_PATH}`;
const YANDEX_POST = `POST ${YANDEX_PATH}`;

// What the page did, read off the log of the fake host.
const posts = (env) => env.log.filter((entry) => entry[0] === 'postJson').map(([, path, body]) => ({ path, body }));
const gets = (env) => env.log.filter((entry) => entry[0] === 'fetchJson').map(([, path, params]) => ({ path, params }));
const errors = (env) => env.log.filter((entry) => entry[0] === 'showError')
  .map(([, message, options]) => ({ message, ...options }));
const cells = (container) => container.parsed.inputs.map((input) => input.value);

const envs = [];
afterEach(() => {
  // An unscripted request would be caught by the editor and shown as an error; none of these scenarios may hit one.
  for (const env of envs.splice(0)) {
    assert.deepEqual(errors(env).filter((error) => /no scripted response/.test(error.message)), []);
    assert.equal(env.log.some((entry) => entry[0] === 'SCENARIO THREW'), false);
  }
});

function page(spec, hostOverrides) {
  const env = makeEnv();
  envs.push(env);
  const impl = createImpl(env, hostOverrides);
  return { env, impl, spec };
}

async function type(container, index, value) {
  const input = container.parsed.inputs[index];
  input.value = value;
  await container.dispatch('input', { target: input });
}

async function change(element, value) {
  element.value = value;
  await element.dispatch('change');
}

async function open(spec, hostOverrides) {
  const ctx = page(spec, hostOverrides);
  ctx.impl.setActiveView(spec.view);
  ctx.env.script(spec.get, ok(spec.rows));
  await spec.filter(ctx.env).load.dispatch('click');
  return ctx;
}

const REVIEW_LOADED = ['5', '', '7'];

test('review: every row is posted with the value the last response showed, and the saved rows replace the cells', async () => {
  const { env } = await open(REVIEW);
  const editor = REVIEW.container(env);
  assert.deepEqual(cells(editor), REVIEW_LOADED);
  assert.deepEqual(REVIEW.state(env), { dirty: false, saving: false });

  await type(editor, 0, '8');
  await type(editor, 1, '1,5');
  assert.equal(REVIEW.state(env).dirty, true);

  env.script(REVIEW.post, ok(withRows(REVIEW.rows, (rows) => { rows[0].value = 8; rows[1].value = 2; })));
  await REVIEW.save(env).dispatch('click');

  assert.deepEqual(posts(env), [{
    path: REVIEW.path,
    body: {
      month: '2026-09',
      company_id: null,
      staff_id: null,
      items: [
        { company_id: 1, staff_id: 10, value: 8, previous_value: 5 },
        { company_id: 1, staff_id: 11, value: 1.5, previous_value: null },
        { company_id: 2, staff_id: 12, value: 7, previous_value: 7 },
      ],
    },
  }]);
  assert.deepEqual(cells(editor), ['8', '2', '7'], 'the response is what the table shows now');
  assert.deepEqual(REVIEW.state(env), { dirty: false, saving: false });
  assert.deepEqual(errors(env), []);

  // The next save carries the saved values as the baseline.
  env.script(REVIEW.post, ok(REVIEW.rows));
  await type(editor, 2, '9');
  await REVIEW.save(env).dispatch('click');
  assert.deepEqual(posts(env)[1].body.items.map((item) => item.previous_value), [8, 2, 7]);
});

test('review: after a month change whose reload failed, every save still goes to the month the rows came from', async () => {
  const { env } = await open(REVIEW);
  const editor = REVIEW.container(env);
  env.script(REVIEW.get, fail('down', 503, 'server_error'));
  await change(REVIEW.filter(env).month, '2026-10');
  assert.deepEqual(cells(editor), REVIEW_LOADED, 'the rows of the loaded month stay');

  env.script(REVIEW.post, ok(REVIEW.rows));
  await type(editor, 0, '8');
  await REVIEW.save(env).dispatch('click');
  env.script(REVIEW.post, ok(REVIEW.rows));
  await type(editor, 0, '9');
  await REVIEW.save(env).dispatch('click');

  assert.deepEqual(posts(env).map((post) => post.body.month), ['2026-09', '2026-09']);
});

test('review: a cleared cell is posted as null, never as zero', async () => {
  const { env } = await open(REVIEW);
  await type(REVIEW.container(env), 0, '');
  env.script(REVIEW.post, ok(REVIEW.rows));
  await REVIEW.save(env).dispatch('click');
  assert.deepEqual(posts(env)[0].body.items.map((item) => [item.value, item.previous_value]), [[null, 5], [null, null], [7, 7]]);
});

test('review: a negative or unreadable cell sends nothing, keeps the draft and says so', async () => {
  const { env } = await open(REVIEW);
  const editor = REVIEW.container(env);
  await type(editor, 0, '-1');
  await REVIEW.save(env).dispatch('click');
  assert.deepEqual(posts(env), []);
  assert.equal(errors(env).length, 1);

  await type(editor, 0, '5');
  await type(editor, 2, 'abc');
  await REVIEW.save(env).dispatch('click');
  assert.deepEqual(posts(env), []);
  assert.equal(errors(env).length, 2);
  assert.equal(editor.parsed.inputs[2].value, 'abc');
  assert.equal(REVIEW.state(env).dirty, true);

  // The refusal comes with a retry that works once the cell is fixed.
  await type(editor, 2, '7');
  env.script(REVIEW.post, ok(REVIEW.rows));
  await env.lastRetry();
  assert.equal(posts(env).length, 1);
});

test('review: when someone else saved first (409), their value wins and what was typed is dropped', async () => {
  const { env } = await open(REVIEW);
  const editor = REVIEW.container(env);
  await type(editor, 0, '9');
  env.script(REVIEW.post, fail('stale', 409));
  env.script(REVIEW.get, ok(withRows(REVIEW.rows, (rows) => { rows[0].value = 6; })));
  await REVIEW.save(env).dispatch('click');

  assert.equal(posts(env).length, 1);
  assert.equal(gets(env).length, 2, 'the editor reloads after the conflict');
  assert.deepEqual(cells(editor), ['6', '', '7']);
  assert.deepEqual(REVIEW.state(env), { dirty: false, saving: false });
  assert.equal(errors(env).length, 1);
  assert.equal(errors(env)[0].apiStatus, 'ready', 'the conflict is explained, not reported as a failure');
  assert.equal(errors(env)[0].retry, false);
});

test('review: a 409 whose reload fails keeps the draft and offers a retry that reloads', async () => {
  const { env } = await open(REVIEW);
  const editor = REVIEW.container(env);
  await type(editor, 0, '9');
  env.script(REVIEW.post, fail('stale', 409));
  env.script(REVIEW.get, fail('down', 503, 'server_error'));
  await REVIEW.save(env).dispatch('click');

  assert.deepEqual(errors(env).map(({ message, retry }) => [message, retry]), [['down', true]]);
  assert.equal(editor.parsed.inputs[0].value, '9');
  assert.equal(REVIEW.state(env).dirty, true);

  env.script(REVIEW.get, ok(REVIEW.rows));
  await env.lastRetry();
  assert.deepEqual(cells(editor), REVIEW_LOADED);
  assert.equal(REVIEW.state(env).dirty, false);
});

test('review: a server error keeps the draft, and the retry posts the same body again', async () => {
  const { env } = await open(REVIEW);
  const editor = REVIEW.container(env);
  await type(editor, 0, '9');
  env.script(REVIEW.post, fail('Server error', 500, 'server_error'));
  await REVIEW.save(env).dispatch('click');

  assert.deepEqual(errors(env).map(({ message, apiStatus, retry }) => [message, apiStatus, retry]), [['Server error', 'server_error', true]]);
  assert.equal(editor.parsed.inputs[0].value, '9');
  assert.equal(REVIEW.state(env).dirty, true);
  assert.equal(REVIEW.state(env).saving, false);

  env.script(REVIEW.post, ok(withRows(REVIEW.rows, (rows) => { rows[0].value = 9; })));
  await env.lastRetry();
  assert.equal(posts(env).length, 2);
  assert.deepEqual(posts(env)[1].body, posts(env)[0].body);
  assert.deepEqual(REVIEW.state(env), { dirty: false, saving: false });
});

test('review: a second click while the save is in flight sends nothing, and the editor is locked until it ends', async () => {
  const { env } = await open(REVIEW);
  await type(REVIEW.container(env), 0, '9');
  env.script(REVIEW.post, { hold: true });
  const first = REVIEW.save(env).dispatch('click');
  await tick();
  const second = REVIEW.save(env).dispatch('click');
  await tick();

  assert.equal(REVIEW.state(env).saving, true);
  assert.equal(REVIEW.save(env).disabled, true);
  assert.equal(REVIEW.container(env).inert, true);
  assert.equal(posts(env).length, 1);

  env.release(REVIEW.post, ok(withRows(REVIEW.rows, (rows) => { rows[0].value = 9; })));
  await Promise.all([first, second]);
  assert.equal(posts(env).length, 1);
  assert.deepEqual(REVIEW.state(env), { dirty: false, saving: false });
  assert.equal(REVIEW.container(env).inert, false);
  assert.deepEqual(cells(REVIEW.container(env)), ['9', '', '7']);
});

test('review: declining the unsaved-changes dialog puts the pickers back and loads nothing; accepting loads the new scope', async () => {
  const { env } = await open(REVIEW);
  const filter = REVIEW.filter(env);
  await type(REVIEW.container(env), 0, '8');

  env.confirmAnswer = false;
  await change(filter.month, '2026-08');
  await change(filter.branch, '2');
  await change(filter.staff, '12');
  await filter.load.dispatch('click');
  assert.equal(env.confirmCalls, 4);
  assert.equal(gets(env).length, 1, 'nothing was loaded');
  assert.deepEqual([filter.month.value, filter.branch.value, filter.staff.value], ['2026-09', '', '']);
  assert.equal(REVIEW.container(env).parsed.inputs[0].value, '8', 'the draft is still there');
  assert.equal(REVIEW.state(env).dirty, true);

  env.confirmAnswer = true;
  env.script(REVIEW.get, ok(REVIEW.rows));
  await change(filter.month, '2026-08');
  assert.deepEqual(gets(env).at(-1).params, { month: '2026-08', company_id: '', staff_id: '' });
  assert.equal(filter.month.value, '2026-08');
  assert.deepEqual(cells(REVIEW.container(env)), REVIEW_LOADED, 'the draft was given up');
  assert.equal(REVIEW.state(env).dirty, false);
});

test('review: while a save is in flight the pickers cannot be moved, and no dialog is asked', async () => {
  const { env } = await open(REVIEW);
  await type(REVIEW.container(env), 0, '8');
  env.script(REVIEW.post, { hold: true });
  const saving = REVIEW.save(env).dispatch('click');
  await tick();
  await change(REVIEW.filter(env).branch, '1');

  assert.equal(REVIEW.filter(env).branch.value, '', 'back where the rows came from');
  assert.equal(gets(env).length, 1);
  assert.equal(env.confirmCalls, 0);

  env.release(REVIEW.post, ok(REVIEW.rows));
  await saving;
});

test('review: a clean editor follows every picker with the right query', async () => {
  const { env } = await open(REVIEW);
  const filter = REVIEW.filter(env);
  for (const [picker, value] of [[filter.month, '2026-07'], [filter.branch, '1'], [filter.staff, '10']]) {
    env.script(REVIEW.get, ok(REVIEW.rows));
    await change(picker, value);
  }
  env.script(REVIEW.get, ok(REVIEW.rows));
  await filter.load.dispatch('click');

  assert.equal(env.confirmCalls, 0);
  assert.deepEqual(gets(env).map((get) => get.params), [
    { month: '2026-09', company_id: '', staff_id: '' },
    { month: '2026-07', company_id: '', staff_id: '' },
    { month: '2026-07', company_id: '1', staff_id: '' },
    { month: '2026-07', company_id: '1', staff_id: '10' },
    { month: '2026-07', company_id: '1', staff_id: '10' },
  ]);
});

test('review: a failed load offers a retry; an empty answer leaves nothing to save; stale and superseded answers raise no error', async () => {
  const { env } = page(REVIEW);
  env.script(REVIEW.get, fail('down', 503, 'server_error'));
  const filter = REVIEW.filter(env);
  await filter.load.dispatch('click');
  assert.deepEqual(errors(env).map(({ message, retry }) => [message, retry]), [['down', true]]);

  env.script(REVIEW.get, ok({ data: { rows: [] } }));
  await env.lastRetry();
  assert.deepEqual(cells(REVIEW.container(env)), []);
  assert.equal(REVIEW.save(env).disabled, true);
  const shown = errors(env).length;

  // An answer for a request that is no longer the current one is not reported as loaded and raises no error,
  // and a superseded request (aborted by a newer one) is silent.
  const loadedStates = () => env.log.filter((entry) => entry[0] === 'setLoadedApiState').length;
  const loaded = loadedStates();
  env.stale.add(env.requestCount + 1);
  env.script(REVIEW.get, ok(REVIEW.rows));
  await filter.load.dispatch('click');
  assert.equal(loadedStates(), loaded);
  assert.equal(errors(env).length, shown);

  env.script(REVIEW.get, fail('gone', 0, 'superseded'));
  await filter.load.dispatch('click');
  assert.equal(errors(env).length, shown);
});

test('review: discarding restores the loaded values and pickers, and does nothing before the first load', async () => {
  const idle = page(REVIEW);
  idle.impl.setActiveView(REVIEW.view);
  assert.doesNotThrow(() => idle.impl.discard());
  assert.deepEqual(gets(idle.env), []);

  const { env, impl } = await open(REVIEW);
  await type(REVIEW.container(env), 0, '8');
  REVIEW.filter(env).month.value = '2026-01';
  impl.discard();
  assert.deepEqual(cells(REVIEW.container(env)), REVIEW_LOADED);
  assert.equal(REVIEW.filter(env).month.value, '2026-09');
  assert.equal(REVIEW.state(env).dirty, false);
});

test('opz: the total column previews the typed addition and ignores rows the reports do not count', async () => {
  const { env } = await open(OPZ);
  const editor = OPZ.container(env);
  const totals = () => editor.parsed.inputs.map((input) => input.row.totalCell.textContent);
  assert.deepEqual(totals(), ['29', '15', '0']);

  await type(editor, 0, '10');
  await type(editor, 1, '5');
  // Row 2 (not counted) keeps its current value; row 3 re-previews its stored addition of 2.
  assert.deepEqual(totals(), ['35', '15', '2']);
  assert.equal(OPZ.state(env).dirty, true);
});

test('opz: the save goes to the OPZ endpoint with previous values, and the response replaces the cells', async () => {
  const { env } = await open(OPZ);
  const editor = OPZ.container(env);
  await type(editor, 0, '10');
  env.script(OPZ.post, ok(withRows(OPZ.rows, (rows) => { rows[0].value = 10; })));
  await OPZ.save(env).dispatch('click');

  assert.deepEqual(posts(env), [{
    path: OPZ.path,
    body: {
      month: '2026-09',
      company_id: null,
      staff_id: null,
      items: [
        { company_id: 1, staff_id: 10, value: 10, previous_value: 4 },
        { company_id: 1, staff_id: 11, value: null, previous_value: null },
        { company_id: 2, staff_id: 12, value: 2, previous_value: 2 },
      ],
    },
  }]);
  assert.deepEqual(cells(editor), ['10', '', '2']);
  assert.deepEqual(OPZ.state(env), { dirty: false, saving: false });
});

const yandexInputs = (env) => env.els.yandexPayEditor.parsed.inputs;
const openYandex = async (response = YANDEX_ROWS) => {
  const ctx = page({ view: 'yandexPayFacts' });
  ctx.impl.setActiveView('yandexPayFacts');
  ctx.env.script(YANDEX_GET, ok(response));
  await ctx.env.filterEls.yandexPay.load.dispatch('click');
  return ctx;
};

test('yandex: typing an amount into an empty row proposes the date, and the save posts the amount with its date', async () => {
  const { env } = await openYandex();
  const editor = env.els.yandexPayEditor;
  // inputs: amount, date, amount, date
  assert.deepEqual(cells(editor), ['2000.25', '2026-09-10', '', '']);

  await type(editor, 2, '1 500,5');
  assert.equal(yandexInputs(env)[3].value, '2026-09-29', 'the date the server would pick');
  await type(editor, 0, '2100');
  assert.equal(yandexInputs(env)[1].value, '2026-09-10', 'a stored date is left alone');

  env.script(YANDEX_POST, ok(withRows(YANDEX_ROWS, (rows) => {
    rows[0].value = 2100;
    rows[1].value = 1500.5;
    rows[1].data_through = '2026-09-29';
  })));
  await env.els.yandexPaySave.dispatch('click');

  assert.deepEqual(posts(env), [{
    path: YANDEX_PATH,
    body: {
      month: '2026-09',
      company_id: null,
      items: [
        {
          company_id: 1, value: 2100, data_through: '2026-09-10',
          previous_value: 2000.25, previous_data_through: '2026-09-10',
        },
        {
          company_id: 2, value: 1500.5, data_through: '2026-09-29',
          previous_value: null, previous_data_through: null,
        },
      ],
    },
  }]);
  assert.deepEqual(cells(editor), ['2100', '2026-09-10', '1500.5', '2026-09-29']);
  assert.deepEqual(env.probe().yandex, { dirty: false, saving: false });
});

test('yandex: emptying an amount empties its date, and every unreadable entry is refused without a request', async () => {
  const { env } = await openYandex();
  const editor = env.els.yandexPayEditor;
  await type(editor, 0, '');
  assert.equal(yandexInputs(env)[1].value, '', 'no value has no date');

  await type(editor, 2, '10');
  const refuse = async (label) => {
    const before = errors(env).length;
    await env.els.yandexPaySave.dispatch('click');
    assert.equal(errors(env).length, before + 1, label);
    assert.deepEqual(posts(env), [], label);
  };
  yandexInputs(env)[3].value = '';
  await editor.dispatch('input', { target: yandexInputs(env)[3] });
  await refuse('an amount without a date');
  yandexInputs(env)[3].validity = { badInput: true };
  await refuse('an unreadable date');
  yandexInputs(env)[3].validity = { badInput: false };
  yandexInputs(env)[3].value = '2026-08-30';
  await refuse('a date outside the month');
  yandexInputs(env)[3].value = '2026-09-29';
  for (const [text, label] of [['12x', 'not a number'], ['1.234', 'too many decimals'], ['-5', 'negative']]) {
    await type(editor, 2, text);
    await refuse(label);
  }
  assert.equal(env.probe().yandex.dirty, true, 'the draft survives every refusal');
});

test('yandex: a 409 reloads the editor and the other value wins', async () => {
  const { env } = await openYandex();
  const editor = env.els.yandexPayEditor;
  await type(editor, 0, '2100');
  env.script(YANDEX_POST, fail('stale', 409));
  env.script(YANDEX_GET, ok(withRows(YANDEX_ROWS, (rows) => { rows[0].value = 7; })));
  await env.els.yandexPaySave.dispatch('click');

  assert.equal(posts(env).length, 1);
  assert.equal(yandexInputs(env)[0].value, '7');
  assert.deepEqual(env.probe().yandex, { dirty: false, saving: false });
  assert.deepEqual(errors(env).map(({ apiStatus, retry }) => [apiStatus, retry]), [['ready', false]]);
});

test('yandex: a month that cannot be edited, or has no branches, cannot be saved', async () => {
  const future = await openYandex(YANDEX_FUTURE);
  await future.env.els.yandexPaySave.dispatch('click');
  assert.deepEqual(posts(future.env), []);
  assert.equal(future.env.els.yandexPayFutureHint.hidden, false);
  assert.equal(yandexInputs(future.env).every((input) => input.disabled), true);

  const empty = await openYandex({ data: { editable: true, rows: [] } });
  await empty.env.els.yandexPaySave.dispatch('click');
  assert.deepEqual(posts(empty.env), []);
  assert.equal(empty.env.els.yandexPayFutureHint.hidden, true);
});

test('yandex: a half-typed month is put back without a request', async () => {
  const { env } = await openYandex();
  await change(env.filterEls.yandexPay.month, '');
  assert.equal(env.filterEls.yandexPay.month.value, '2026-09');
  assert.equal(gets(env).length, 1);
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

test('the editor takes no input while its save is in flight and takes it again afterwards', async () => {
  const env = makeEnv();
  const impl = createImpl(env);
  impl.setActiveView('reviewFacts');
  const rows = { data: { rows: [{ company_id: 1, staff_id: 10, staff_name: 'Ann', value: 1 }] } };
  env.script('GET /dashboard/plan/reviews_fact', { ok: rows });
  await env.filterEls.reviewFacts.load.dispatch('click');
  env.els.reviewFactEditor.parsed.inputs[0].value = '2';
  await env.els.reviewFactEditor.dispatch('input', { target: env.els.reviewFactEditor.parsed.inputs[0] });

  env.script('POST /dashboard/plan/reviews_fact', { hold: true });
  const saving = env.els.reviewFactSave.dispatch('click');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(env.els.reviewFactEditor.inert, true);

  env.release('POST /dashboard/plan/reviews_fact', { ok: rows });
  await saving;
  assert.equal(env.els.reviewFactEditor.inert, false);
});
