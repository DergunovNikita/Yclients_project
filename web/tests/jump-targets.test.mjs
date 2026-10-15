import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  activeTargetIndex,
  collectJumpTargets,
  jumpTargetsSignature,
  shouldShowJumpNav,
} from '../src/jumpTargets.js';

const element = (key, label) => ({
  getAttribute: (name) => ({ 'data-jump-target': key, 'data-jump-label': label })[name] ?? null,
});
const root = (...elements) => ({ querySelectorAll: () => elements });

test('targets keep document order and carry their label', () => {
  const a = element('a', 'Первый');
  const b = element('b', ' Второй ');
  const targets = collectJumpTargets(root(a, b), () => true);
  assert.deepEqual(targets.map(({ key, label }) => [key, label]), [['a', 'Первый'], ['b', 'Второй']]);
  assert.equal(targets[1].element, b);
});

test('unrendered, unlabelled and duplicate blocks are not offered', () => {
  const hidden = element('hidden', 'Скрыт');
  const targets = collectJumpTargets(
    root(element('a', 'A'), hidden, element('nolabel', ''), element('a', 'Дубль'), element('', 'Без ключа')),
    (node) => node !== hidden,
  );
  assert.deepEqual(targets.map((target) => target.key), ['a']);
});

test('a missing root yields no targets', () => {
  assert.deepEqual(collectJumpTargets(null), []);
});

test('the nav needs at least two targets', () => {
  assert.equal(shouldShowJumpNav([]), false);
  assert.equal(shouldShowJumpNav([{}]), false);
  assert.equal(shouldShowJumpNav([{}, {}]), true);
});

test('signature changes with keys and labels only', () => {
  const one = [{ key: 'a', label: 'A', element: 1 }, { key: 'b', label: 'B', element: 2 }];
  const same = one.map((target) => ({ ...target, element: 9 }));
  assert.equal(jumpTargetsSignature(one), jumpTargetsSignature(same));
  assert.notEqual(jumpTargetsSignature(one), jumpTargetsSignature([{ ...one[0], label: 'Z' }, one[1]]));
});

test('the current block is the last one that reached the offset', () => {
  assert.equal(activeTargetIndex([]), -1);
  assert.equal(activeTargetIndex([300, 900]), 0);
  assert.equal(activeTargetIndex([-400, 100, 700]), 1);
  assert.equal(activeTargetIndex([-400, -100, 80]), 2);
});

test('at the page bottom the last block wins', () => {
  assert.equal(activeTargetIndex([-400, 200, 500], { atBottom: true }), 2);
});

test('every jump label key exists in all locales', () => {
  const keys = ['dash.jumpNavLabel', 'dash.jumpPlanBranches', 'dash.jumpPlanStaff', 'reports.jumpMetrics', 'reports.jumpCharts'];
  for (const locale of ['ru', 'en', 'it']) {
    const messages = JSON.parse(readFileSync(new URL(`../locales/${locale}.json`, import.meta.url), 'utf8'));
    for (const key of keys) {
      const value = key.split('.').reduce((node, part) => node?.[part], messages);
      assert.equal(typeof value, 'string', `${locale}: ${key}`);
    }
  }
});
