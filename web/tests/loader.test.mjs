import assert from 'node:assert/strict';
import test from 'node:test';

import { createLatestRequestScope } from '../src/dashboardRequestState.js';

import {
  CAPTION_INTERVAL_MS,
  CAPTION_KEYS,
  createLoaderTiming,
  MIN_VISIBLE_MS,
  mountLoader,
  SHOW_DELAY_MS,
  stopLoaderWithRequest,
} from '../src/loader.js';

// One fake clock that also drives the timers, so `now()` and the scheduled callbacks never disagree.
function fakeClock() {
  let time = 0;
  let nextId = 0;
  const timers = new Map();
  return {
    now: () => time,
    setTimer(callback, delay) {
      const id = ++nextId;
      timers.set(id, { callback, at: time + delay });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    get timerCount() { return timers.size; },
    advance(ms) {
      const target = time + ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        time = due[1].at;
        due[1].callback();
      }
      time = target;
    },
  };
}

function recordedTiming() {
  const clock = fakeClock();
  const events = [];
  const timing = createLoaderTiming({
    onShow: (mode) => events.push(['show', mode, clock.now()]),
    onHide: () => events.push(['hide', clock.now()]),
    onCaption: (index) => events.push(['caption', index, clock.now()]),
    onModeChange: (mode) => events.push(['mode', mode, clock.now()]),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    now: clock.now,
  });
  return { clock, events, timing };
}

test('a load that ends before the delay never shows anything', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS - 1);
  timing.stop();
  clock.advance(10000);
  assert.deepEqual(events, []);
  assert.equal(timing.isVisible(), false);
  assert.equal(clock.timerCount, 0);
});

test('the loader appears after the show delay with the first caption', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS - 1);
  assert.deepEqual(events, []);
  clock.advance(1);
  assert.deepEqual(events, [['show', 'first', SHOW_DELAY_MS], ['caption', 0, SHOW_DELAY_MS]]);
  assert.equal(timing.isVisible(), true);
});

test('once shown it stays for the minimum visible time', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('refresh');
  const shownFor = MIN_VISIBLE_MS - 50;
  clock.advance(SHOW_DELAY_MS + shownFor);
  timing.stop();
  const remaining = MIN_VISIBLE_MS - shownFor;
  clock.advance(remaining - 1);
  assert.equal(timing.isVisible(), true);
  clock.advance(1);
  assert.deepEqual(events.at(-1), ['hide', SHOW_DELAY_MS + MIN_VISIBLE_MS]);
  assert.equal(timing.isVisible(), false);
  assert.equal(clock.timerCount, 0);
});

test('a load that outlasts the minimum hides at once', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  const longLoad = SHOW_DELAY_MS + MIN_VISIBLE_MS * 3;
  clock.advance(longLoad);
  timing.stop();
  assert.deepEqual(events.at(-1), ['hide', longLoad]);
  assert.equal(clock.timerCount, 0);
});

test('captions rotate on the caption interval through every key and wrap', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS + CAPTION_INTERVAL_MS * CAPTION_KEYS.length);
  const indexes = events.filter(([kind]) => kind === 'caption').map(([, index]) => index);
  assert.deepEqual(indexes, [...CAPTION_KEYS.keys(), 0]);
  const times = events.filter(([kind]) => kind === 'caption').map(([, , at]) => at);
  assert.deepEqual(times.slice(0, 3), [0, 1, 2].map((n) => SHOW_DELAY_MS + n * CAPTION_INTERVAL_MS));
});

test('each new show starts again from the first caption', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS + CAPTION_INTERVAL_MS * 2);
  timing.stop();
  clock.advance(10);
  timing.start('first');
  clock.advance(SHOW_DELAY_MS);
  assert.deepEqual(events.at(-1).slice(0, 2), ['caption', 0]);
});

test('start while visible keeps it on screen without a new delay or caption reset', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS + CAPTION_INTERVAL_MS);
  const before = events.length;
  timing.start('first');
  assert.equal(events.length, before);
  assert.equal(timing.isVisible(), true);
  clock.advance(CAPTION_INTERVAL_MS);
  assert.deepEqual(events.slice(before), [['caption', 2, SHOW_DELAY_MS + CAPTION_INTERVAL_MS * 2]]);
});

test('start during a pending hide cancels it', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('refresh');
  clock.advance(SHOW_DELAY_MS + 50);
  timing.stop();
  clock.advance(100);
  timing.start('refresh');
  clock.advance(10000);
  assert.equal(events.some(([kind]) => kind === 'hide'), false);
  assert.equal(timing.isVisible(), true);
  timing.stop();
  assert.deepEqual(events.at(-1), ['hide', SHOW_DELAY_MS + 50 + 100 + 10000]);
});

test('a first-mode start during a pending hide resumes the captions', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS + 50);
  timing.stop();
  clock.advance(100);
  timing.start('first');
  clock.advance(CAPTION_INTERVAL_MS);
  assert.deepEqual(events.at(-1), ['caption', 1, SHOW_DELAY_MS + 50 + 100 + CAPTION_INTERVAL_MS]);
  timing.stop();
  assert.equal(clock.timerCount, 0);
});

test('start while the delay is pending does not restart it', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS - 100);
  timing.start('first');
  clock.advance(100);
  assert.deepEqual(events[0], ['show', 'first', SHOW_DELAY_MS]);
});

test('the refresh mode has no caption timers', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('refresh');
  clock.advance(SHOW_DELAY_MS);
  assert.deepEqual(events, [['show', 'refresh', SHOW_DELAY_MS]]);
  assert.equal(clock.timerCount, 0);
  clock.advance(CAPTION_INTERVAL_MS * 3);
  assert.deepEqual(events, [['show', 'refresh', SHOW_DELAY_MS]]);
});

test('the mode switches while visible, and captions follow the first mode only', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('refresh');
  clock.advance(SHOW_DELAY_MS);
  timing.start('first');
  assert.deepEqual(events.slice(1), [['mode', 'first', SHOW_DELAY_MS], ['caption', 0, SHOW_DELAY_MS]]);
  clock.advance(CAPTION_INTERVAL_MS);
  assert.deepEqual(events.at(-1), ['caption', 1, SHOW_DELAY_MS + CAPTION_INTERVAL_MS]);
  timing.start('refresh');
  assert.deepEqual(events.at(-1), ['mode', 'refresh', SHOW_DELAY_MS + CAPTION_INTERVAL_MS]);
  clock.advance(CAPTION_INTERVAL_MS * 2);
  assert.equal(events.filter(([kind]) => kind === 'caption').length, 2);
});

test('the mode chosen while the delay is pending is the one shown', () => {
  const { clock, events, timing } = recordedTiming();
  timing.start('refresh');
  clock.advance(100);
  timing.start('first');
  clock.advance(SHOW_DELAY_MS - 100);
  assert.deepEqual(events[0], ['show', 'first', SHOW_DELAY_MS]);
});

test('stop clears every timer', () => {
  const { clock, timing } = recordedTiming();
  timing.start('first');
  clock.advance(SHOW_DELAY_MS + CAPTION_INTERVAL_MS + 10);
  timing.stop();
  clock.advance(MIN_VISIBLE_MS);
  assert.equal(clock.timerCount, 0);
  timing.stop();
  assert.equal(clock.timerCount, 0);
});

// A DOM stand-in just big enough for mountLoader: it only needs createElement, attributes and text.
function fakeNode(tag = 'div') {
  const attributes = new Map();
  return {
    tag,
    className: '',
    dataset: {},
    hidden: true,
    textContent: '',
    children: [],
    offsetWidth: 0,
    classList: {
      classes: new Set(),
      add(name) { this.classes.add(name); },
      remove(name) { this.classes.delete(name); },
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    removeAttribute(name) { attributes.delete(name); },
    getAttribute: (name) => attributes.get(name) ?? null,
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
  };
}

const statusOf = (el) => el.children.find((child) => child.getAttribute('role') === 'status');
const captionOf = (el) => el.children.find((child) => child.tag === 'p');

async function withFakeDocument(run) {
  const original = globalThis.document;
  globalThis.document = { createElement: fakeNode };
  try {
    await run();
  } finally {
    globalThis.document = original;
  }
}

test('mountLoader draws the placeholder and reads captions through t()', async () => {
  await withFakeDocument(() => {
    const clock = fakeClock();
    let locale = 'a';
    const t = (key) => `${locale}:${key}`;
    const el = fakeNode();
    const loader = mountLoader(el, { t, ...clock });
    const caption = captionOf(el);
    const status = statusOf(el);

    loader.start('first');
    assert.equal(el.hidden, true);
    assert.equal(status.getAttribute('role'), 'status');

    clock.advance(SHOW_DELAY_MS);
    assert.equal(el.hidden, false);
    assert.equal(el.dataset.mode, 'first');
    assert.equal(status.textContent, 'a:common.loading');
    assert.equal(caption.getAttribute('aria-hidden'), 'true');
    assert.equal(caption.textContent, 'a:loader.trim');

    locale = 'b';
    clock.advance(CAPTION_INTERVAL_MS);
    assert.equal(caption.textContent, 'b:loader.columns');

    loader.stop();
    assert.equal(el.hidden, true);
    assert.equal(clock.timerCount, 0);
  });
});

// Timers the test fires by hand, to see the state between the un-hide and the zero-delay status write.
function manualTimers() {
  const timers = [];
  return {
    timers,
    setTimer(callback, delay) {
      const timer = { callback, delay, cleared: false, fired: false };
      timers.push(timer);
      return timer;
    },
    clearTimer(timer) { timer.cleared = true; },
    pending: () => timers.filter((timer) => !timer.cleared && !timer.fired),
    fire(delay) {
      const timer = timers.find((entry) => entry.delay === delay && !entry.cleared && !entry.fired);
      timer.fired = true;
      timer.callback();
    },
  };
}

test('the status text is written one timer tick after the placeholder is un-hidden', async () => {
  await withFakeDocument(() => {
    const manual = manualTimers();
    const el = fakeNode();
    const loader = mountLoader(el, { t: (key) => key, ...manual, now: () => 0 });
    const status = statusOf(el);

    loader.start('first');
    manual.fire(SHOW_DELAY_MS);
    assert.equal(el.hidden, false);
    assert.equal(status.textContent, '');
    assert.equal(manual.pending().some((timer) => timer.delay === 0), true);

    manual.fire(0);
    assert.equal(status.textContent, 'common.loading');
  });
});

test('a stop right after the show flushes the pending status and leaves no status timer behind', async () => {
  await withFakeDocument(() => {
    const manual = manualTimers();
    const el = fakeNode();
    const loader = mountLoader(el, { t: (key) => key, ...manual, now: () => 0 });
    const status = statusOf(el);

    loader.start('refresh');
    manual.fire(SHOW_DELAY_MS);
    loader.stop();
    assert.equal(manual.pending().some((timer) => timer.delay === 0), false);
    assert.equal(status.textContent, 'common.loading');

    manual.fire(MIN_VISIBLE_MS);
    assert.equal(el.hidden, true);
    assert.equal(status.textContent, '');
    assert.equal(manual.pending().length, 0);
  });
});

test('mountLoader hides and clears the status after the minimum, and is null-safe', async () => {
  await withFakeDocument(() => {
    const clock = fakeClock();
    const el = fakeNode();
    const loader = mountLoader(el, { t: (key) => key, ...clock });
    loader.start('refresh');
    clock.advance(SHOW_DELAY_MS);
    assert.equal(el.dataset.mode, 'refresh');
    loader.stop();
    clock.advance(MIN_VISIBLE_MS);
    assert.equal(el.hidden, true);
    assert.equal(statusOf(el).textContent, '');

    // restarting after a hide works
    assert.doesNotThrow(() => {
      loader.start('first');
      loader.stop();
    });
    const detached = mountLoader(null, { t: (key) => key });
    assert.doesNotThrow(() => {
      detached.start('first');
      detached.stop();
    });
  });
});

test('a superseded request finishing late does not stop the loader of its successor', () => {
  const scope = createLatestRequestScope();
  let stops = 0;
  const begin = () => stopLoaderWithRequest(scope.start(), () => { stops += 1; });
  const first = begin();
  const second = begin();
  assert.equal(first.signal.aborted, true);
  first.finish();
  assert.equal(stops, 0);
  assert.equal(second.isCurrent(), true);
  second.finish();
  assert.equal(stops, 1);
  assert.equal(second.isCurrent(), false);
});

test('an aborted scope leaves the stop to whoever aborted it', () => {
  const scope = createLatestRequestScope();
  let stops = 0;
  const request = stopLoaderWithRequest(scope.start(), () => { stops += 1; });
  scope.abort();
  request.finish();
  assert.equal(stops, 0);
});
