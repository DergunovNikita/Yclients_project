import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.localStorage = { getItem: () => 'ru' };

const drafts = await import('../src/editorDrafts.js');
const { applyPlanSettingsCells, planSettingsTypedCells } = await import('../src/planSettings.js');
const { createManualEditors } = await import('../src/manualEditors.js');
const { createUnsavedChangesGuard } = await import('../src/unsavedChanges.js');
const { OPZ_ROWS, REVIEW_ROWS, YANDEX_ROWS, makeEnv } = await import('./helpers/manual-editor-env.mjs');

const WHO = { userId: 7, accountId: 3 };

class MemoryStorage {
  constructor() { this.map = new Map(); }
  get length() { return this.map.size; }
  key(index) { return [...this.map.keys()][index] ?? null; }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null; }
  setItem(key, value) { this.map.set(key, String(value)); }
  removeItem(key) { this.map.delete(key); }
}

function manualTimers() {
  const timers = new Map();
  let next = 1;
  return {
    setTimer: (fn) => { timers.set(next, fn); next += 1; return next - 1; },
    clearTimer: (id) => timers.delete(id),
    fire: () => [...timers.keys()].forEach((id) => { const fn = timers.get(id); timers.delete(id); fn(); }),
    count: () => timers.size,
  };
}

function fakeDocument() {
  const make = () => ({
    children: [],
    listeners: {},
    className: '',
    textContent: '',
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    append(...nodes) { this.children.push(...nodes); },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    remove() { this.removed = true; },
  });
  return { createElement: make };
}

function makeController({ storage = new MemoryStorage(), identity = () => WHO, timers = manualTimers() } = {}) {
  const doc = fakeDocument();
  const controller = drafts.createDraftController({
    identity, storage, doc, now: () => 1000, setTimer: timers.setTimer, clearTimer: timers.clearTimer,
  });
  return { controller, storage, timers, doc };
}

test('a draft key names the person, the tenant, the editor and the scope', () => {
  const key = drafts.draftKey(WHO, 'reviewFacts', { month: '2026-09', branch: '', staff: '12' });
  assert.equal(key, 'portal-draft/v1/7/3/reviewFacts/branch=&month=2026-09&staff=12');
  assert.deepEqual(drafts.parseDraftKey(key), {
    userId: '7', accountId: '3', editorId: 'reviewFacts', scope: 'branch=&month=2026-09&staff=12',
  });
  assert.equal(drafts.parseDraftKey('something-else'), null);
  // The order of the scope's fields must not matter.
  assert.equal(
    drafts.draftKey(WHO, 'x', { staff: 1, month: 'a' }),
    drafts.draftKey(WHO, 'x', { month: 'a', staff: 1 }),
  );
});

test('keys of different people, tenants and months never collide', () => {
  const keys = [
    drafts.draftKey({ userId: 1, accountId: 3 }, 'e', { month: '2026-09' }),
    drafts.draftKey({ userId: 2, accountId: 3 }, 'e', { month: '2026-09' }),
    drafts.draftKey({ userId: 1, accountId: 4 }, 'e', { month: '2026-09' }),
    drafts.draftKey({ userId: 1, accountId: 3 }, 'e', { month: '2026-10' }),
  ];
  assert.equal(new Set(keys).size, 4);
});

test('the fingerprint tells different server data apart and is stable', () => {
  assert.equal(drafts.fingerprint('abc'), drafts.fingerprint('abc'));
  assert.notEqual(drafts.fingerprint('abc'), drafts.fingerprint('abd'));
  assert.notEqual(drafts.fingerprint(''), drafts.fingerprint('a'));
});

test('only rows that differ from what the server drew are kept', () => {
  const rows = [
    { key: 'a', cells: { value: '5' }, server: { value: '5' }, previous: { value: 5 } },
    { key: 'b', cells: { value: '' }, server: { value: '' }, previous: { value: null } },
    { key: 'c', cells: { value: '9' }, server: { value: '' }, previous: { value: null } },
    { key: 'd', cells: { value: '' }, server: { value: '4' }, previous: { value: 4 } },
  ];
  assert.deepEqual(drafts.changedRows(rows), [
    { key: 'c', cells: { value: '9' }, previous: { value: null } },
    { key: 'd', cells: { value: '' }, previous: { value: 4 } },
  ]);
});

test('a row is restored only over the value it was typed over', () => {
  const draft = [
    { key: 'same', cells: { value: '1' }, previous: { value: 5 } },
    { key: 'moved', cells: { value: '2' }, previous: { value: 5 } },
    { key: 'was-empty', cells: { value: '3' }, previous: { value: null } },
    { key: 'now-filled', cells: { value: '4' }, previous: { value: null } },
    { key: 'gone', cells: { value: '5' }, previous: { value: 1 } },
  ];
  const current = [
    { key: 'same', previous: { value: 5 } },
    { key: 'moved', previous: { value: 6 } },
    { key: 'was-empty', previous: { value: null } },
    { key: 'now-filled', previous: { value: 0 } },
  ];
  const { apply, dropped } = drafts.decideDraftRestore(draft, current);
  assert.deepEqual(apply.map((row) => row.key), ['same', 'was-empty']);
  assert.equal(dropped, 3);
});

test('an empty cell (null) and a typed zero are different server states', () => {
  const draft = [{ key: 'a', cells: { value: '1' }, previous: { value: null } }];
  assert.equal(drafts.decideDraftRestore(draft, [{ key: 'a', previous: { value: 0 } }]).apply.length, 0);
  assert.equal(drafts.decideDraftRestore(draft, [{ key: 'a', previous: { value: null } }]).apply.length, 1);
});

test('a Yandex Pay row needs both the amount and the date to be unchanged', () => {
  const draft = [{ key: '1', cells: { value: '9', date: '' }, previous: { value: 5, data_through: '2026-09-10' } }];
  const moved = [{ key: '1', previous: { value: 5, data_through: '2026-09-11' } }];
  assert.equal(drafts.decideDraftRestore(draft, moved).apply.length, 0);
});

test('a draft is written after the debounce, once, from what the form holds then', () => {
  const { controller, storage, timers } = makeController();
  let reads = 0;
  controller.schedule('e', { month: 'm' }, () => ({ n: (reads += 1) }));
  controller.schedule('e', { month: 'm' }, () => ({ n: (reads += 1) }));
  assert.equal(storage.length, 0);
  assert.equal(timers.count(), 1, 'a second keystroke replaces the first timer');
  timers.fire();
  assert.equal(reads, 1);
  assert.deepEqual(controller.read('e', { month: 'm' }), { n: 1 });
});

test('flush writes what is waiting, a null build removes the draft', () => {
  const { controller, storage, timers } = makeController();
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  controller.flush();
  assert.equal(storage.length, 1);
  assert.equal(timers.count(), 0);
  controller.schedule('e', { month: 'm' }, () => null);
  controller.flush();
  assert.equal(storage.length, 0);
});

test('a cancelled write never happens', () => {
  const { controller, storage } = makeController();
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  controller.cancel('e');
  controller.flush();
  assert.equal(storage.length, 0);
});

test('without a known person nothing is written or read', () => {
  const { controller, storage } = makeController({ identity: () => null });
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  controller.flush();
  assert.equal(storage.length, 0);
  assert.equal(controller.read('e', { month: 'm' }), null);
});

test('a draft is read only under the same person, tenant and scope', () => {
  const { controller, storage } = makeController();
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  controller.flush();
  assert.deepEqual(controller.read('e', { month: 'm' }), { rows: [1] });
  assert.equal(controller.read('e', { month: 'other' }), null);
  assert.equal(controller.read('f', { month: 'm' }), null);
  const other = drafts.createDraftController({ identity: () => ({ userId: 8, accountId: 3 }), storage });
  assert.equal(other.read('e', { month: 'm' }), null);
});

test('garbage in storage is ignored, not thrown', () => {
  const { controller, storage } = makeController();
  storage.setItem(drafts.draftKey(WHO, 'e', { month: 'm' }), '{not json');
  assert.equal(controller.read('e', { month: 'm' }), null);
  storage.setItem(drafts.draftKey(WHO, 'e', { month: 'm' }), JSON.stringify({ v: 99, data: {} }));
  assert.equal(controller.read('e', { month: 'm' }), null);
});

test('a storage that throws never breaks typing', () => {
  const broken = {
    get length() { throw new Error('blocked'); },
    key: () => { throw new Error('blocked'); },
    getItem: () => { throw new Error('blocked'); },
    setItem: () => { throw new Error('quota'); },
    removeItem: () => { throw new Error('blocked'); },
  };
  const { controller } = makeController({ storage: broken });
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  assert.doesNotThrow(() => controller.flush());
  assert.equal(controller.read('e', { month: 'm' }), null);
  assert.doesNotThrow(() => controller.purgeForeign());
  assert.doesNotThrow(() => controller.clearAll());
});

test('another person logging in purges what the previous one left', () => {
  const storage = new MemoryStorage();
  const first = makeController({ storage }).controller;
  first.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  first.flush();
  storage.setItem('unrelated', 'keep');

  const second = makeController({ storage, identity: () => ({ userId: 8, accountId: 3 }) }).controller;
  second.schedule('e', { month: 'm' }, () => ({ rows: [2] }));
  second.flush();
  second.purgeForeign();
  assert.deepEqual(second.read('e', { month: 'm' }), { rows: [2] });
  assert.equal(first.read('e', { month: 'm' }), null);
  assert.equal(storage.getItem('unrelated'), 'keep');
});

test('an explicit logout clears every draft and nothing else', () => {
  const { controller, storage } = makeController();
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  controller.flush();
  storage.setItem('portal_account_id', '3');
  controller.schedule('f', { month: 'm' }, () => ({ rows: [2] }));
  controller.clearAll();
  controller.flush();
  assert.equal(storage.length, 1);
  assert.equal(storage.getItem('portal_account_id'), '3');
});

test('the page-level triggers flush waiting writes', () => {
  const { controller, storage } = makeController();
  const listeners = {};
  const target = { addEventListener: (type, fn) => { listeners[type] = fn; } };
  drafts.bindDraftFlush(controller, target);
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  listeners['portal:session-transition']();
  assert.equal(storage.length, 1);
  controller.schedule('e', { month: 'n' }, () => ({ rows: [1] }));
  listeners.pagehide();
  assert.equal(storage.length, 2);
});

test('a session transition keeps the drafts except when the person logged out on purpose', () => {
  const { controller, storage } = makeController();
  const listeners = {};
  drafts.bindDraftFlush(controller, { addEventListener: (type, fn) => { listeners[type] = fn; } });
  controller.schedule('e', { month: 'm' }, () => ({ rows: [1] }));
  listeners['portal:session-transition']({ detail: { reason: 'idle' } });
  controller.schedule('f', { month: 'm' }, () => ({ rows: [2] }));
  listeners['portal:session-transition']({ detail: { reason: 'ended' } });
  assert.equal(storage.length, 2);
  controller.schedule('g', { month: 'm' }, () => ({ rows: [3] }));
  listeners['portal:session-transition']({ detail: { reason: 'logout' } });
  assert.equal(storage.length, 0, 'a waiting write is dropped too, not stored after the wipe');
});

test('the notice offers a discard only for a restored draft', () => {
  const { controller, doc } = makeController();
  const inserted = [];
  const anchor = { parentNode: { insertBefore: (el) => inserted.push(el) } };
  let discarded = 0;
  controller.showNotice(anchor, 'restored', { onDiscard: () => { discarded += 1; } });
  assert.match(inserted[0].children[0].textContent, /черновик/);
  inserted[0].children[1].listeners.click();
  assert.equal(discarded, 1);
  assert.equal(inserted[0].removed, true);

  controller.showNotice(anchor, 'outdated', { onDiscard: () => { discarded += 1; } });
  inserted[1].children[1].listeners.click();
  assert.equal(discarded, 1, 'dismissing an outdated draft discards nothing more');
  assert.ok(doc);
});

function planInput(dataset, value, defaultValue) {
  return { dataset, value, defaultValue };
}

test('plan settings: only typed cells are drafted and they go back to the same inputs', () => {
  const inputs = [
    planInput({ planBranch: '', companyId: '1', field: 'wax_pct' }, '12', '10'),
    planInput({ planBranch: '', companyId: '1', field: 'opz_pct' }, '5', '5'),
    planInput({ planStaff: '', companyId: '1', staffId: '9', field: 'clients' }, ' 40 ', ''),
    { dataset: {}, value: 'x', defaultValue: '' },
  ];
  const cells = planSettingsTypedCells(inputs);
  assert.deepEqual(cells, { 'branch:1:wax_pct': '12', 'staff:1:9:clients': '40' });

  const fresh = [
    planInput({ planBranch: '', companyId: '1', field: 'wax_pct' }, '10', '10'),
    planInput({ planBranch: '', companyId: '1', field: 'opz_pct' }, '5', '5'),
    planInput({ planStaff: '', companyId: '1', staffId: '9', field: 'clients' }, '', ''),
  ];
  assert.equal(applyPlanSettingsCells(fresh, cells), 2);
  assert.deepEqual(fresh.map((input) => input.value), ['12', '5', '40']);
  assert.equal(applyPlanSettingsCells(fresh, { 'branch:99:wax_pct': '1' }), 0);
});

// ---- the editors, end to end against the scripted page ----

function openPage(storage, hostOverrides = {}) {
  const env = makeEnv();
  const timers = manualTimers();
  const doc = fakeDocument();
  const notices = [];
  for (const editorEl of [env.els.reviewFactEditor, env.els.opzFactEditor, env.els.yandexPayEditor]) {
    editorEl.parentNode = { insertBefore: (el) => notices.push(el) };
  }
  const controller = drafts.createDraftController({
    identity: () => WHO, storage, doc, setTimer: timers.setTimer, clearTimer: timers.clearTimer,
  });
  let activeView = 'overview';
  let editors;
  const guard = createUnsavedChangesGuard({
    isDirty: () => Boolean(editors.byView[activeView]?.dirty),
    isBlocked: () => false,
    confirmLeave: env.host.confirmLeave,
    onDiscard: () => editors.byView[activeView]?.discard(),
  });
  editors = createManualEditors({
    els: env.els,
    filterEls: env.filterEls,
    host: { ...env.host, drafts: controller, guard, filterDropdowns: env.customFilterDropdowns, ...hostOverrides },
  });
  env.probe = () => ({});
  return {
    env, controller, timers, editors, notices,
    open: (view) => { activeView = view; },
  };
}

const ok = (value) => ({ ok: structuredClone(value) });
const reviewGet = 'GET /dashboard/plan/reviews_fact';
const reviewPost = 'POST /dashboard/plan/reviews_fact';

async function loadReview(page, rows = REVIEW_ROWS) {
  page.open('reviewFacts');
  page.env.script(reviewGet, ok(rows));
  await page.env.filterEls.reviewFacts.load.dispatch('click');
}

async function type(page, container, index, value) {
  const input = container.parsed.inputs[index];
  input.value = value;
  await container.dispatch('input', { target: input });
}

const withValue = (rows, staffId, value) => {
  const copy = structuredClone(rows);
  copy.data.rows.find((row) => row.staff_id === staffId).value = value;
  return copy;
};

test('typed review values come back after a reload as a dirty, announced draft', async () => {
  const storage = new MemoryStorage();
  const first = openPage(storage);
  await loadReview(first);
  await type(first, first.env.els.reviewFactEditor, 0, '9');
  await type(first, first.env.els.reviewFactEditor, 1, '3');
  assert.equal(storage.length, 0, 'nothing is written before the debounce');
  first.timers.fire();
  assert.equal(storage.length, 1);
  const stored = JSON.parse(storage.getItem(storage.key(0)));
  assert.deepEqual(stored.data.rows.map((row) => [row.key, row.cells.value, row.previous.value]), [
    ['1:10', '9', 5],
    ['1:11', '3', null],
  ]);
  assert.match(storage.key(0), /\/reviewFacts\/branch=&month=2026-09&staff=$/);

  const second = openPage(storage);
  await loadReview(second);
  const inputs = second.env.els.reviewFactEditor.parsed.inputs;
  assert.deepEqual(inputs.map((input) => input.value), ['9', '3', '7']);
  assert.equal(second.editors.review.dirty, true);
  assert.equal(second.notices.length, 1);
  assert.equal(second.editors.review.savedSnapshot !== '', true);
});

test('a row someone else changed meanwhile is not restored over their value', async () => {
  const storage = new MemoryStorage();
  const first = openPage(storage);
  await loadReview(first);
  await type(first, first.env.els.reviewFactEditor, 0, '9');
  await type(first, first.env.els.reviewFactEditor, 1, '3');
  first.controller.flush();

  const second = openPage(storage);
  await loadReview(second, withValue(REVIEW_ROWS, 10, 6));
  const inputs = second.env.els.reviewFactEditor.parsed.inputs;
  assert.deepEqual(inputs.map((input) => input.value), ['6', '3', '7'], 'the other row still comes back');
  assert.equal(second.notices[0].children[0].textContent.includes('1'), true, 'the notice counts the dropped row');
});

test('a draft whose every row moved is dropped with an outdated notice and stays clean', async () => {
  const storage = new MemoryStorage();
  const first = openPage(storage);
  await loadReview(first);
  await type(first, first.env.els.reviewFactEditor, 0, '9');
  first.controller.flush();

  const second = openPage(storage);
  await loadReview(second, withValue(REVIEW_ROWS, 10, 6));
  assert.equal(second.env.els.reviewFactEditor.parsed.inputs[0].value, '6');
  assert.equal(second.editors.review.dirty, false);
  assert.equal(storage.length, 0);
  assert.equal(second.notices.length, 1);
});

test('a draft for another month is not applied to this one', async () => {
  const storage = new MemoryStorage();
  const first = openPage(storage);
  await loadReview(first);
  await type(first, first.env.els.reviewFactEditor, 0, '9');
  first.controller.flush();

  const second = openPage(storage);
  second.env.filterEls.reviewFacts.month.value = '2026-08';
  await loadReview(second);
  assert.equal(second.env.els.reviewFactEditor.parsed.inputs[0].value, '5');
  assert.equal(second.editors.review.dirty, false);
  assert.equal(storage.length, 1, 'the September draft is still waiting for September');
});

test('restoring never sends anything: the save still carries previous_value from the server', async () => {
  const storage = new MemoryStorage();
  const first = openPage(storage);
  await loadReview(first);
  await type(first, first.env.els.reviewFactEditor, 0, '9');
  first.controller.flush();

  const second = openPage(storage);
  await loadReview(second);
  assert.equal(second.env.log.some((entry) => entry[0] === 'postJson'), false);
  second.env.script(reviewPost, ok(withValue(REVIEW_ROWS, 10, 9)));
  await second.env.els.reviewFactSave.dispatch('click');
  const post = second.env.log.find((entry) => entry[0] === 'postJson');
  const item = post[2].items.find((row) => row.staff_id === 10);
  assert.deepEqual([item.value, item.previous_value], [9, 5]);
  const untouched = post[2].items.find((row) => row.staff_id === 11);
  assert.deepEqual([untouched.value, untouched.previous_value], [null, null], 'an empty cell stays null, not 0');
});

test('a saved draft is cleared', async () => {
  const storage = new MemoryStorage();
  const page = openPage(storage);
  await loadReview(page);
  await type(page, page.env.els.reviewFactEditor, 0, '9');
  page.controller.flush();
  assert.equal(storage.length, 1);
  page.env.script(reviewPost, ok(withValue(REVIEW_ROWS, 10, 9)));
  await page.env.els.reviewFactSave.dispatch('click');
  assert.equal(storage.length, 0);
});

test('a 409 clears the draft and the reload does not bring it back', async () => {
  const storage = new MemoryStorage();
  const page = openPage(storage);
  await loadReview(page);
  await type(page, page.env.els.reviewFactEditor, 0, '9');
  page.controller.flush();
  page.env.script(reviewPost, { error: { message: 'conflict', status: 409 } });
  page.env.script(reviewGet, ok(withValue(REVIEW_ROWS, 10, 6)));
  await page.env.els.reviewFactSave.dispatch('click');
  assert.equal(storage.length, 0);
  assert.equal(page.env.els.reviewFactEditor.parsed.inputs[0].value, '6');
  assert.equal(page.editors.review.dirty, false);
});

test('discarding the restored draft puts the server values back and forgets it', async () => {
  const storage = new MemoryStorage();
  const first = openPage(storage);
  await loadReview(first);
  await type(first, first.env.els.reviewFactEditor, 0, '9');
  first.controller.flush();

  const second = openPage(storage);
  await loadReview(second);
  assert.equal(second.editors.review.dirty, true);
  second.notices[0].children[1].listeners.click();
  assert.equal(second.env.els.reviewFactEditor.parsed.inputs[0].value, '5');
  assert.equal(second.editors.review.dirty, false);
  assert.equal(storage.length, 0);
});

test('leaving through the guard discards the draft', async () => {
  const storage = new MemoryStorage();
  const page = openPage(storage);
  await loadReview(page);
  await type(page, page.env.els.reviewFactEditor, 0, '9');
  page.controller.flush();
  page.env.script(reviewGet, ok(REVIEW_ROWS));
  page.env.filterEls.reviewFacts.month.value = '2026-08';
  await page.env.filterEls.reviewFacts.month.dispatch('change');
  assert.equal(storage.length, 0);
});

test('editing a value back to the stored one leaves no draft', async () => {
  const storage = new MemoryStorage();
  const page = openPage(storage);
  await loadReview(page);
  await type(page, page.env.els.reviewFactEditor, 0, '9');
  page.controller.flush();
  assert.equal(storage.length, 1);
  await type(page, page.env.els.reviewFactEditor, 0, '5');
  page.controller.flush();
  assert.equal(storage.length, 0);
});

test('OPZ drafts restore the cells and refresh the totals preview', async () => {
  const storage = new MemoryStorage();
  const get = 'GET /dashboard/plan/opz_fact';
  const first = openPage(storage);
  first.open('opzFacts');
  first.env.script(get, ok(OPZ_ROWS));
  await first.env.filterEls.opzFacts.load.dispatch('click');
  await type(first, first.env.els.opzFactEditor, 0, '10');
  first.controller.flush();

  const second = openPage(storage);
  second.open('opzFacts');
  second.env.script(get, ok(OPZ_ROWS));
  await second.env.filterEls.opzFacts.load.dispatch('click');
  assert.equal(second.env.els.opzFactEditor.parsed.inputs[0].value, '10');
  assert.equal(second.editors.opz.dirty, true);
  assert.match(second.env.els.opzFactMeta.textContent, /доп\. 12 · итого 52/, 'the typed 10 reaches the preview: 10 + 0 + 2');
});

test('Yandex Pay restores the amount and the date together, and refuses a moved pair', async () => {
  const storage = new MemoryStorage();
  const get = 'GET /dashboard/payments/yandex_pay';
  const first = openPage(storage);
  first.open('yandexPayFacts');
  first.env.script(get, ok(YANDEX_ROWS));
  await first.env.filterEls.yandexPay.load.dispatch('click');
  const editor = first.env.els.yandexPayEditor;
  await type(first, editor, 0, '2100');
  editor.parsed.inputs[1].value = '2026-09-12';
  await editor.dispatch('input', { target: editor.parsed.inputs[1] });
  first.controller.flush();

  const second = openPage(storage);
  second.open('yandexPayFacts');
  second.env.script(get, ok(YANDEX_ROWS));
  await second.env.filterEls.yandexPay.load.dispatch('click');
  const inputs = second.env.els.yandexPayEditor.parsed.inputs;
  assert.deepEqual([inputs[0].value, inputs[1].value], ['2100', '2026-09-12']);
  assert.equal(second.editors.yandex.dirty, true);

  const third = openPage(storage);
  third.open('yandexPayFacts');
  const moved = structuredClone(YANDEX_ROWS);
  moved.data.rows[0].data_through = '2026-09-11';
  third.env.script(get, ok(moved));
  await third.env.filterEls.yandexPay.load.dispatch('click');
  assert.equal(third.env.els.yandexPayEditor.parsed.inputs[0].value, '2000.25');
  assert.equal(third.editors.yandex.dirty, false);
});

test('without a drafts service the editors are exactly what they were', async () => {
  const env = makeEnv();
  const guard = createUnsavedChangesGuard({
    isDirty: () => false, isBlocked: () => false, confirmLeave: env.host.confirmLeave, onDiscard: () => {},
  });
  const editors = createManualEditors({
    els: env.els, filterEls: env.filterEls, host: { ...env.host, guard, filterDropdowns: env.customFilterDropdowns },
  });
  env.script(reviewGet, ok(REVIEW_ROWS));
  await env.filterEls.reviewFacts.load.dispatch('click');
  await type({}, env.els.reviewFactEditor, 0, '9');
  assert.equal(editors.review.dirty, true);
});
