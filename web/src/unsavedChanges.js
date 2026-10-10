export function createUnsavedChangesGuard({
  isDirty,
  isBlocked = () => false,
  confirmLeave,
  onDiscard,
}) {
  let decisionPending = false;

  async function run(action) {
    if (isBlocked() || decisionPending) return false;
    if (!isDirty()) {
      await action();
      return true;
    }

    decisionPending = true;
    try {
      const shouldLeave = await confirmLeave();
      if (!shouldLeave) return false;
      onDiscard();
      await action();
      return true;
    } finally {
      decisionPending = false;
    }
  }

  return { run };
}

export function editorSaveDockState({
  activeView,
  planSettingsDirty,
  planSettingsSaving,
  reviewFactDirty,
  reviewFactSaving,
  opzFactDirty,
  opzFactSaving,
  yandexPayDirty = false,
  yandexPaySaving = false,
  isDemo = false,
}) {
  const editors = {
    planSettings: {
      dirty: planSettingsDirty,
      saving: planSettingsSaving,
    },
    reviewFacts: {
      dirty: reviewFactDirty,
      saving: reviewFactSaving,
    },
    opzFacts: {
      dirty: opzFactDirty,
      saving: opzFactSaving,
    },
    yandexPayFacts: {
      dirty: yandexPayDirty,
      saving: yandexPaySaving,
    },
  };
  const editor = editors[activeView];
  if (!editor || isDemo) return { editor: null, visible: false, saving: false };
  return {
    editor: activeView,
    visible: Boolean(editor.dirty || editor.saving),
    saving: Boolean(editor.saving),
  };
}

export function historyNavigationDecision({
  targetPosition,
  currentPosition,
  restorationPosition = null,
  handlingNavigation = false,
  needsGuard = false,
}) {
  const target = Number(targetPosition);
  const current = Number(currentPosition);
  const restoration = restorationPosition === null ? null : Number(restorationPosition);

  if (Number.isFinite(restoration)) {
    if (Number.isFinite(target) && target === restoration) {
      return { type: 'completeRestoration', position: target };
    }
    return {
      type: 'restore',
      delta: Number.isFinite(target) ? restoration - target : 0,
    };
  }

  if (!Number.isFinite(target) && needsGuard) {
    return { type: 'guardUnknown' };
  }

  const delta = Number.isFinite(target) && Number.isFinite(current) ? current - target : 0;
  if (handlingNavigation) return { type: 'restore', delta };
  if (needsGuard && delta) return { type: 'guard', delta };
  return {
    type: 'navigate',
    position: Number.isFinite(target) ? target : null,
  };
}

export function shouldHandleSameTabNavigation({
  button = 0,
  altKey = false,
  ctrlKey = false,
  metaKey = false,
  shiftKey = false,
}) {
  return button === 0 && !altKey && !ctrlKey && !metaKey && !shiftKey;
}

/**
 * Whether this event is the one that should re-check an editor's draft.
 *
 * Checkboxes and selects fire both `input` and `change` for one click, text cells fire `input` per keystroke
 * and `change` once more on blur. Listening to both ran the whole-table draft comparison twice per click;
 * taking `change` for toggles and `input` for typed cells runs it once for each.
 */
export function editEventChecksDraft(event) {
  const toggle = Boolean(event.target?.matches?.('select, input[type="checkbox"]'));
  return (event.type === 'change') === toggle;
}
