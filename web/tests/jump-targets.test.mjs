import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activeTargetIndex,
  collectJumpTargets,
  effectiveTop,
  jumpTargetsSignature,
  mutationsAffectJumpTargets,
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

test('loader caption toggles do not rebuild the nav', () => {
  const inLoader = { target: { closest: (selector) => (selector === '.view-loader' ? {} : null) } };
  const elsewhere = { target: { closest: () => null } };
  assert.equal(mutationsAffectJumpTargets([inLoader, inLoader]), false);
  assert.equal(mutationsAffectJumpTargets([inLoader, elsewhere]), true);
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

test('a block parked under sticky filters is current even though its margin exceeds the offset', () => {
  // Two-line filter row: the jumped-to block rests at 182px (margin), the previous one is far above.
  const tops = [-600, 182, 900];
  assert.equal(activeTargetIndex(tops), 0);
  const lowered = tops.map((top, index) => effectiveTop(top, index === 1 ? 182 : 0));
  assert.equal(activeTargetIndex(lowered), 1);
  // No margin, or one below the offset, changes nothing; a block still below its parking spot is not current.
  assert.equal(effectiveTop(300, 0), 300);
  assert.equal(effectiveTop(300, 84), 300);
  assert.equal(effectiveTop(300, Number.NaN), 300);
  assert.equal(activeTargetIndex([-600, 182 + 40, 900].map((top, index) => effectiveTop(top, index === 1 ? 182 : 0))), 0);
});
