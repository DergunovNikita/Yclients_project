import { OPZ_ROWS, REVIEW_ROWS, YANDEX_FUTURE, YANDEX_ROWS, makeEnv } from './manual-editor-env.mjs';

const REVIEW_GET = 'GET /dashboard/plan/reviews_fact';
const REVIEW_POST = 'POST /dashboard/plan/reviews_fact';
const OPZ_GET = 'GET /dashboard/plan/opz_fact';
const OPZ_POST = 'POST /dashboard/plan/opz_fact';
const YANDEX_GET = 'GET /dashboard/payments/yandex_pay';
const YANDEX_POST = 'POST /dashboard/payments/yandex_pay';

const ok = (value) => ({ ok: structuredClone(value) });
const fail = (message, status, apiStatus) => ({ error: { message, status, apiStatus } });
const tick = () => new Promise((resolve) => { setImmediate(resolve); });

const withRows = (source, edit) => {
  const copy = structuredClone(source);
  edit(copy.data.rows);
  return copy;
};

async function typeInto(env, container, index, value) {
  const input = container.parsed.inputs[index];
  input.value = value;
  await container.dispatch('input', { target: input });
}

async function change(element, value) {
  element.value = value;
  await element.dispatch('change');
}

// Review and OPZ editors are the same machine with different names and endpoints.
const FACT = {
  review: {
    view: 'reviewFacts',
    get: REVIEW_GET,
    post: REVIEW_POST,
    rows: REVIEW_ROWS,
    filter: (env) => env.filterEls.reviewFacts,
    container: (env) => env.els.reviewFactEditor,
    save: (env) => env.els.reviewFactSave,
  },
  opz: {
    view: 'opzFacts',
    get: OPZ_GET,
    post: OPZ_POST,
    rows: OPZ_ROWS,
    filter: (env) => env.filterEls.opzFacts,
    container: (env) => env.els.opzFactEditor,
    save: (env) => env.els.opzFactSave,
  },
};

function factScenarios(name, spec) {
  const open = async (env, impl) => {
    impl.setActiveView(spec.view);
    env.script(spec.get, ok(spec.rows));
    await spec.filter(env).load.dispatch('click');
    env.snapshot('loaded');
  };
  const saved = (edit) => ok(withRows(spec.rows, edit));
  const scenarios = {};

  scenarios[`${name}: load, edit, save`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '8');
    await typeInto(env, spec.container(env), 1, '1,5');
    env.snapshot('edited');
    env.script(spec.post, saved((rows) => { rows[0].value = 8; rows[1].value = 2; }));
    await spec.save(env).dispatch('click');
    env.snapshot('saved');
    env.script(spec.post, ok(spec.rows));
    await spec.save(env).dispatch('click');
    env.snapshot('saved twice');
  };

  scenarios[`${name}: clearing a cell and an unreadable cell`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '');
    env.snapshot('cleared');
    await typeInto(env, spec.container(env), 2, 'abc');
    env.snapshot('garbage');
    await spec.save(env).dispatch('click');
    env.snapshot('rejected');
    await env.lastRetry?.();
    env.snapshot('retried');
  };

  scenarios[`${name}: negative value is refused before any request`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '-1');
    await spec.save(env).dispatch('click');
    env.snapshot('negative');
  };

  scenarios[`${name}: someone else saved first (409) and the reload works`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '9');
    env.script(spec.post, fail('stale', 409));
    env.script(spec.get, saved((rows) => { rows[0].value = 6; }));
    await spec.save(env).dispatch('click');
    env.snapshot('conflict');
  };

  scenarios[`${name}: 409 and the reload fails, then retry`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '9');
    env.script(spec.post, fail('stale', 409));
    env.script(spec.get, fail('down', 503, 'server_error'));
    await spec.save(env).dispatch('click');
    env.snapshot('reload failed');
    env.script(spec.get, ok(spec.rows));
    await env.lastRetry();
    env.snapshot('retried');
  };

  scenarios[`${name}: server error keeps the draft and offers a retry`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '9');
    env.script(spec.post, fail('Server error', 500, 'server_error'));
    await spec.save(env).dispatch('click');
    env.snapshot('failed');
    env.script(spec.post, saved((rows) => { rows[0].value = 9; }));
    await env.lastRetry();
    env.snapshot('retried');
  };

  scenarios[`${name}: a second click while saving is ignored`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '9');
    env.script(spec.post, { hold: true });
    const first = spec.save(env).dispatch('click');
    await tick();
    env.snapshot('saving');
    const second = spec.save(env).dispatch('click');
    await tick();
    env.release(spec.post, ok(withRows(spec.rows, (rows) => { rows[0].value = 9; })));
    await Promise.all([first, second]);
    env.snapshot('done');
  };

  scenarios[`${name}: unsaved changes guard declines, then accepts`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '8');
    env.confirmAnswer = false;
    await change(spec.filter(env).month, '2026-08');
    env.snapshot('declined month');
    await change(spec.filter(env).branch, '2');
    env.snapshot('declined branch');
    await change(spec.filter(env).staff, '12');
    env.snapshot('declined staff');
    await spec.filter(env).load.dispatch('click');
    env.snapshot('declined refresh');
    env.confirmAnswer = true;
    env.script(spec.get, ok(spec.rows));
    await change(spec.filter(env).month, '2026-08');
    env.snapshot('accepted month');
  };

  scenarios[`${name}: a save in flight blocks navigation`] = async (env, impl) => {
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '8');
    env.script(spec.post, { hold: true });
    const saving = spec.save(env).dispatch('click');
    await tick();
    await change(spec.filter(env).branch, '1');
    env.snapshot('blocked');
    env.release(spec.post, ok(spec.rows));
    await saving;
    env.snapshot('settled');
  };

  scenarios[`${name}: clean editor follows the pickers`] = async (env, impl) => {
    await open(env, impl);
    env.script(spec.get, ok(spec.rows));
    await change(spec.filter(env).month, '2026-07');
    env.script(spec.get, ok(spec.rows));
    await change(spec.filter(env).branch, '1');
    env.script(spec.get, ok(spec.rows));
    await change(spec.filter(env).staff, '10');
    env.script(spec.get, ok(spec.rows));
    await spec.filter(env).load.dispatch('click');
    env.snapshot('end');
  };

  scenarios[`${name}: load failures and stale responses`] = async (env, impl) => {
    impl.setActiveView(spec.view);
    env.script(spec.get, fail('down', 503, 'server_error'));
    await spec.filter(env).load.dispatch('click');
    env.snapshot('failed');
    env.script(spec.get, ok({ data: { rows: [] } }));
    await env.lastRetry();
    env.snapshot('empty');
    env.stale.add(env.requestCount + 1);
    env.script(spec.get, ok(spec.rows));
    await spec.filter(env).load.dispatch('click');
    env.snapshot('stale ok');
    env.stale.add(env.requestCount + 1);
    env.script(spec.get, fail('gone', 0, 'superseded'));
    await spec.filter(env).load.dispatch('click');
    env.script(spec.get, fail('gone', 0, 'superseded'));
    await spec.filter(env).load.dispatch('click');
    env.snapshot('superseded');
  };

  scenarios[`${name}: discard restores what was loaded`] = async (env, impl) => {
    impl.setActiveView(spec.view);
    impl.discard();
    env.snapshot('discard before any load');
    await open(env, impl);
    await typeInto(env, spec.container(env), 0, '8');
    spec.filter(env).month.value = '2026-01';
    impl.discard();
    env.snapshot('discard after edit');
  };

  return scenarios;
}

const yandex = (env) => env.filterEls.yandexPay;

async function openYandex(env, impl, response = YANDEX_ROWS) {
  impl.setActiveView('yandexPayFacts');
  env.script(YANDEX_GET, ok(response));
  await yandex(env).load.dispatch('click');
  env.snapshot('loaded');
}

const yandexSaved = (edit) => ok(withRows(YANDEX_ROWS, edit));

export const SCENARIOS = {
  ...factScenarios('review', FACT.review),
  ...factScenarios('opz', FACT.opz),

  'yandex: load, type an amount (the date follows), save': async (env, impl) => {
    await openYandex(env, impl);
    const editor = env.els.yandexPayEditor;
    // inputs: amount, date, amount, date
    await typeInto(env, editor, 2, '1 500,5');
    env.snapshot('typed into the empty row');
    await typeInto(env, editor, 0, '2100');
    env.snapshot('changed the amount');
    env.script(YANDEX_POST, yandexSaved((rows) => { rows[0].value = 2100; rows[1].value = 1500.5; rows[1].data_through = '2026-09-29'; }));
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('saved');
  },

  'yandex: emptying the amount empties the date, a bad date is refused': async (env, impl) => {
    await openYandex(env, impl);
    const editor = env.els.yandexPayEditor;
    await typeInto(env, editor, 0, '');
    env.snapshot('amount cleared');
    await typeInto(env, editor, 2, '10');
    editor.parsed.inputs[3].value = '';
    await editor.dispatch('input', { target: editor.parsed.inputs[3] });
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('date required');
    editor.parsed.inputs[3].validity = { badInput: true };
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('unreadable date');
    editor.parsed.inputs[3].validity = { badInput: false };
    editor.parsed.inputs[3].value = '2026-08-30';
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('date outside the month');
    await typeInto(env, editor, 2, '12x');
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('not a number');
    await typeInto(env, editor, 2, '1.234');
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('too precise');
    await typeInto(env, editor, 2, '-5');
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('negative');
  },

  'yandex: 409 with a working and with a failing reload': async (env, impl) => {
    await openYandex(env, impl);
    const editor = env.els.yandexPayEditor;
    await typeInto(env, editor, 0, '2100');
    env.script(YANDEX_POST, fail('stale', 409));
    env.script(YANDEX_GET, yandexSaved((rows) => { rows[0].value = 7; }));
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('conflict');
    await typeInto(env, editor, 0, '2200');
    env.script(YANDEX_POST, fail('stale', 409));
    env.script(YANDEX_GET, fail('down', 503, 'server_error'));
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('reload failed');
    env.script(YANDEX_GET, ok(YANDEX_ROWS));
    await env.lastRetry();
    env.snapshot('retried');
  },

  'yandex: server error, retry, double click': async (env, impl) => {
    await openYandex(env, impl);
    const editor = env.els.yandexPayEditor;
    await typeInto(env, editor, 0, '2100');
    env.script(YANDEX_POST, fail('Server error', 500, 'server_error'));
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('failed');
    env.script(YANDEX_POST, { hold: true });
    const first = env.lastRetry();
    await tick();
    const second = env.els.yandexPaySave.dispatch('click');
    await tick();
    env.snapshot('saving');
    env.release(YANDEX_POST, yandexSaved((rows) => { rows[0].value = 2100; }));
    await Promise.all([first, second]);
    env.snapshot('done');
  },

  'yandex: a month that is not editable cannot be saved': async (env, impl) => {
    await openYandex(env, impl, YANDEX_FUTURE);
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('future month');
    await openYandex(env, impl, { data: { editable: true, rows: [] } });
    await env.els.yandexPaySave.dispatch('click');
    env.snapshot('no branches');
  },

  'yandex: pickers, empty month and the guard': async (env, impl) => {
    await openYandex(env, impl);
    const editor = env.els.yandexPayEditor;
    await change(yandex(env).month, '');
    env.snapshot('half-typed month');
    await typeInto(env, editor, 0, '2100');
    env.confirmAnswer = false;
    await change(yandex(env).month, '2026-08');
    await change(yandex(env).branch, '2');
    await yandex(env).load.dispatch('click');
    env.snapshot('declined');
    env.confirmAnswer = true;
    env.script(YANDEX_GET, ok(YANDEX_ROWS));
    await change(yandex(env).month, '2026-08');
    env.script(YANDEX_GET, ok(YANDEX_ROWS));
    await change(yandex(env).branch, '2');
    env.snapshot('accepted');
  },

  'yandex: load failures, stale responses and discard': async (env, impl) => {
    impl.setActiveView('yandexPayFacts');
    impl.discard();
    env.snapshot('discard before any load');
    env.script(YANDEX_GET, fail('down', 503, 'server_error'));
    await yandex(env).load.dispatch('click');
    env.snapshot('failed');
    env.script(YANDEX_GET, ok(YANDEX_ROWS));
    await env.lastRetry();
    env.stale.add(env.requestCount + 1);
    env.script(YANDEX_GET, ok(YANDEX_ROWS));
    await yandex(env).load.dispatch('click');
    env.script(YANDEX_GET, fail('gone', 0, 'superseded'));
    await yandex(env).load.dispatch('click');
    await typeInto(env, env.els.yandexPayEditor, 0, '5');
    yandex(env).month.value = '2026-01';
    impl.discard();
    env.snapshot('discarded');
  },
};

/** Runs every scenario on a fresh page; `createImpl(env)` wires the implementation under test to it. */
export async function runScenarios(createImpl) {
  const traces = {};
  for (const [name, scenario] of Object.entries(SCENARIOS)) {
    const env = makeEnv();
    const impl = createImpl(env);
    try {
      await scenario(env, impl);
    } catch (error) {
      env.log.push(['SCENARIO THREW', String(error?.message)]);
    }
    // The markup is kept readable once per editor, so a drift shows up as a legible diff.
    if (/: load, edit, save$|^yandex: load, type/.test(name)) env.log.push(['markup', env.markup()]);
    traces[name] = env.log;
  }
  return traces;
}
