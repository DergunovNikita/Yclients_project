import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;

class MemoryStorage {
  constructor() {
    this.store = new Map();
  }

  getItem(key) {
    return this.store.has(key) ? this.store.get(key) : null;
  }

  setItem(key, value) {
    this.store.set(key, String(value));
  }
}

async function loadIdleModule() {
  const server = await createServer({
    appType: 'custom',
    logLevel: 'silent',
    root: new URL('..', import.meta.url).pathname,
    server: { middlewareMode: true },
  });
  return { idle: await server.ssrLoadModule('/src/idleSession.js'), server };
}

function tab(idle, clock, storage, log = []) {
  const monitor = idle.createIdleMonitor({
    now: () => clock.t,
    storage,
    noteThrottleMs: 0,
    writeThrottleMs: 0,
    onPrompt: (remaining) => log.push(['prompt', remaining]),
    onResume: () => log.push(['resume']),
    onExpire: () => log.push(['expire']),
  });
  return { monitor, log };
}

test('the thresholds live in one place: prompt after 2 h, logout 5 minutes later', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());

  assert.equal(idle.IDLE_PROMPT_AFTER_MS, 2 * HOUR);
  assert.equal(idle.IDLE_LOGOUT_AFTER_MS, 2 * HOUR + 5 * MINUTE);
});

test('idle time walks active -> prompt with a countdown -> expired', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const { monitor, log } = tab(idle, clock, new MemoryStorage());

  clock.t += 2 * HOUR - 1;
  monitor.tick();
  assert.deepEqual(log, []);

  clock.t += 1;
  monitor.tick();
  assert.deepEqual(log, [['prompt', 5 * MINUTE]]);

  clock.t += 3 * MINUTE;
  monitor.tick();
  assert.deepEqual(log.at(-1), ['prompt', 2 * MINUTE]);

  clock.t += 2 * MINUTE;
  monitor.tick();
  assert.deepEqual(log.at(-1), ['expire']);
  monitor.tick();
  assert.equal(log.filter(([kind]) => kind === 'expire').length, 1, 'expiry fires once');
});

test('activity before the prompt pushes the deadline out', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const { monitor, log } = tab(idle, clock, new MemoryStorage());

  clock.t += 119 * MINUTE;
  monitor.noteActivity();
  clock.t += 119 * MINUTE;
  monitor.tick();

  assert.deepEqual(log, []);
});

test('activity in any tab keeps every other tab from prompting', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const storage = new MemoryStorage();
  const background = tab(idle, clock, storage);
  const active = tab(idle, clock, storage);

  clock.t += 119 * MINUTE;
  active.monitor.noteActivity();
  clock.t += 119 * MINUTE;
  background.monitor.tick();

  assert.deepEqual(background.log, [], 'the idle tab sees the other tab working');
});

test('once the prompt threshold passed, only the button counts as presence', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const { monitor, log } = tab(idle, clock, new MemoryStorage());

  // A laptop slept for 3 hours and somebody nudges the mouse before the next tick.
  clock.t += 3 * HOUR;
  monitor.noteActivity();
  assert.deepEqual(log, [['expire']], 'the nudge does not rescue the session');
});

test('activity is ignored while the prompt is up; confirming resets the clock', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const { monitor, log } = tab(idle, clock, new MemoryStorage());

  clock.t += 2 * HOUR + MINUTE;
  monitor.tick();
  clock.t += MINUTE;
  monitor.noteActivity();
  monitor.tick();
  assert.deepEqual(log.at(-1), ['prompt', 3 * MINUTE], 'the countdown keeps running');

  assert.equal(monitor.confirm(), true);
  assert.deepEqual(log.at(-1), ['resume']);
  clock.t += 119 * MINUTE;
  monitor.tick();
  assert.equal(monitor.phase(), 'active');
});

test('confirming after the window ran out is too late', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const { monitor, log } = tab(idle, clock, new MemoryStorage());

  clock.t += 2 * HOUR + 6 * MINUTE;
  assert.equal(monitor.confirm(), false);
  assert.deepEqual(log, [['expire']]);
  assert.equal(monitor.confirm(), false);
});

test('confirming in one tab closes the prompt of the others', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const storage = new MemoryStorage();
  const first = tab(idle, clock, storage);
  const second = tab(idle, clock, storage);

  clock.t += 2 * HOUR + MINUTE;
  first.monitor.tick();
  second.monitor.tick();
  first.monitor.confirm();
  second.monitor.tick();

  assert.deepEqual(second.log.map(([kind]) => kind), ['prompt', 'resume']);
  assert.equal(second.monitor.phase(), 'active');
});

test('a clock moved backwards reads as activity, never as negative idle time', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 10 * HOUR };
  const { monitor, log } = tab(idle, clock, new MemoryStorage());

  clock.t -= 5 * HOUR;
  monitor.tick();
  clock.t += 2 * HOUR - 1;
  monitor.tick();

  assert.deepEqual(log, []);
});

test('without usable shared storage the tab still measures its own idle time', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());
  const clock = { t: 1_000_000 };
  const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const { monitor, log } = tab(idle, clock, broken);

  clock.t += 2 * HOUR;
  monitor.tick();

  assert.deepEqual(log, [['prompt', 5 * MINUTE]]);
});

test('the countdown reads as minutes and padded seconds', async (t) => {
  const { idle, server } = await loadIdleModule();
  t.after(() => server.close());

  assert.equal(idle.formatCountdown(5 * MINUTE), '5:00');
  assert.equal(idle.formatCountdown(61_000), '1:01');
  assert.equal(idle.formatCountdown(59_001), '1:00');
  assert.equal(idle.formatCountdown(-5), '0:00');
});
