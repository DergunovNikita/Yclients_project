import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createUnsavedChangesGuard,
  editorSaveDockState,
  historyNavigationDecision,
  shouldHandleSameTabNavigation,
} from '../src/unsavedChanges.js';

test('clean forms navigate without confirmation', async () => {
  let confirmations = 0;
  let navigations = 0;
  const guard = createUnsavedChangesGuard({
    isDirty: () => false,
    confirmLeave: async () => {
      confirmations += 1;
      return false;
    },
    onDiscard: () => {},
  });

  assert.equal(await guard.run(async () => { navigations += 1; }), true);
  assert.equal(confirmations, 0);
  assert.equal(navigations, 1);
});

test('staying preserves dirty state and cancels navigation', async () => {
  let dirty = true;
  let navigations = 0;
  const guard = createUnsavedChangesGuard({
    isDirty: () => dirty,
    confirmLeave: async () => false,
    onDiscard: () => { dirty = false; },
  });

  assert.equal(await guard.run(async () => { navigations += 1; }), false);
  assert.equal(dirty, true);
  assert.equal(navigations, 0);
});

test('leaving discards the draft before navigation', async () => {
  let dirty = true;
  const events = [];
  const guard = createUnsavedChangesGuard({
    isDirty: () => dirty,
    confirmLeave: async () => true,
    onDiscard: () => {
      dirty = false;
      events.push('discard');
    },
  });

  assert.equal(await guard.run(async () => { events.push('navigate'); }), true);
  assert.equal(dirty, false);
  assert.deepEqual(events, ['discard', 'navigate']);
});

test('pending saves and duplicate decisions block navigation', async () => {
  let resolveDecision;
  let blocked = true;
  let navigations = 0;
  const guard = createUnsavedChangesGuard({
    isDirty: () => true,
    isBlocked: () => blocked,
    confirmLeave: () => new Promise((resolve) => { resolveDecision = resolve; }),
    onDiscard: () => {},
  });

  assert.equal(await guard.run(async () => { navigations += 1; }), false);
  blocked = false;
  const first = guard.run(async () => { navigations += 1; });
  assert.equal(await guard.run(async () => { navigations += 1; }), false);
  resolveDecision(true);
  assert.equal(await first, true);
  assert.equal(navigations, 1);
});

test('floating save dock follows the active editor dirty and saving state', () => {
  const base = {
    activeView: 'planSettings',
    planSettingsDirty: false,
    planSettingsSaving: false,
    reviewFactDirty: false,
    reviewFactSaving: false,
  };

  assert.deepEqual(editorSaveDockState(base), {
    editor: 'planSettings',
    visible: false,
    saving: false,
  });
  assert.deepEqual(editorSaveDockState({ ...base, planSettingsDirty: true }), {
    editor: 'planSettings',
    visible: true,
    saving: false,
  });
  assert.deepEqual(editorSaveDockState({ ...base, planSettingsSaving: true }), {
    editor: 'planSettings',
    visible: true,
    saving: true,
  });
  assert.deepEqual(editorSaveDockState({
    ...base,
    activeView: 'reviewFacts',
    reviewFactDirty: true,
  }), {
    editor: 'reviewFacts',
    visible: true,
    saving: false,
  });
  assert.deepEqual(editorSaveDockState({
    ...base,
    activeView: 'opzFacts',
    opzFactDirty: true,
  }), {
    editor: 'opzFacts',
    visible: true,
    saving: false,
  });
  assert.deepEqual(editorSaveDockState({
    ...base,
    activeView: 'opzFacts',
    opzFactSaving: true,
  }), {
    editor: 'opzFacts',
    visible: true,
    saving: true,
  });
});

test('floating save dock stays hidden outside editors and in demo mode', () => {
  const dirtyPlan = {
    activeView: 'overview',
    planSettingsDirty: true,
    planSettingsSaving: false,
    reviewFactDirty: false,
    reviewFactSaving: false,
  };

  assert.deepEqual(editorSaveDockState(dirtyPlan), {
    editor: null,
    visible: false,
    saving: false,
  });
  assert.deepEqual(editorSaveDockState({
    ...dirtyPlan,
    activeView: 'planSettings',
    isDemo: true,
  }), {
    editor: null,
    visible: false,
    saving: false,
  });
});

test('repeated history traversals restore the guarded position instead of navigating', () => {
  assert.deepEqual(historyNavigationDecision({
    targetPosition: 3,
    currentPosition: 5,
    handlingNavigation: true,
    needsGuard: true,
  }), {
    type: 'restore',
    delta: 2,
  });

  assert.deepEqual(historyNavigationDecision({
    targetPosition: 2,
    currentPosition: 5,
    restorationPosition: 5,
    handlingNavigation: true,
    needsGuard: true,
  }), {
    type: 'restore',
    delta: 3,
  });

  assert.deepEqual(historyNavigationDecision({
    targetPosition: 5,
    currentPosition: 5,
    restorationPosition: 5,
    handlingNavigation: true,
    needsGuard: true,
  }), {
    type: 'completeRestoration',
    position: 5,
  });
});

test('untagged history entries cannot bypass a dirty-form guard', () => {
  assert.deepEqual(historyNavigationDecision({
    targetPosition: Number.NaN,
    currentPosition: 5,
    needsGuard: true,
  }), {
    type: 'guardUnknown',
  });
  assert.deepEqual(historyNavigationDecision({
    targetPosition: Number.NaN,
    currentPosition: 5,
    needsGuard: false,
  }), {
    type: 'navigate',
    position: null,
  });
});

test('same-tab navigation guard preserves modified link clicks', () => {
  assert.equal(shouldHandleSameTabNavigation({ button: 0 }), true);
  assert.equal(shouldHandleSameTabNavigation({ button: 1 }), false);
  assert.equal(shouldHandleSameTabNavigation({ button: 0, metaKey: true }), false);
  assert.equal(shouldHandleSameTabNavigation({ button: 0, ctrlKey: true }), false);
  assert.equal(shouldHandleSameTabNavigation({ button: 0, shiftKey: true }), false);
  assert.equal(shouldHandleSameTabNavigation({ button: 0, altKey: true }), false);
});
