// Drafts of what a person has typed into an editor and not saved yet, kept in sessionStorage so a reload, an
// expired session or an idle logout does not cost the work. A draft is never applied blindly: it carries what the
// server showed when it was typed (`previous` per manual-fact row, a fingerprint for the plan), and is restored
// only against the same server state — the optimistic locking of the editors stays the only judge of a save.
import { t } from './i18n.js';

const DRAFT_DEBOUNCE_MS = 500;
const KEY_PREFIX = 'portal-draft/v1';
const DRAFT_VERSION = 1;

/** Storage key: who typed it, for which tenant, into which editor, for which loaded scope. */
export function draftKey({ userId, accountId }, editorId, scope) {
  return [
    KEY_PREFIX,
    encodeURIComponent(String(userId)),
    encodeURIComponent(String(accountId ?? '')),
    editorId,
    scopeKey(scope),
  ].join('/');
}

/** Stable, order-independent form of a scope such as `{ month, branch, staff }`. */
function scopeKey(scope) {
  return Object.keys(scope || {})
    .sort()
    .map((name) => `${name}=${encodeURIComponent(String(scope[name] ?? ''))}`)
    .join('&');
}

export function parseDraftKey(key) {
  if (typeof key !== 'string' || !key.startsWith(`${KEY_PREFIX}/`)) return null;
  const parts = key.slice(KEY_PREFIX.length + 1).split('/');
  if (parts.length < 4) return null;
  try {
    return {
      userId: decodeURIComponent(parts[0]),
      accountId: decodeURIComponent(parts[1]),
      editorId: parts[2],
      scope: parts.slice(3).join('/'),
    };
  } catch {
    return null;
  }
}

/** Short, deterministic digest (FNV-1a) of the server data a draft was typed against. */
export function fingerprint(text) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `${text.length.toString(36)}-${hash.toString(36)}`;
}

const sameJson = (left, right) => JSON.stringify(left) === JSON.stringify(right);

/**
 * Rows worth keeping: those whose cells differ from what the server drew.
 *
 * @param rows  `[{ key, cells, server, previous }]` — `previous` is the value the row carried in the last
 *              response, i.e. what the save's `previous_value` would be.
 */
export function changedRows(rows) {
  return rows
    .filter((row) => !sameJson(row.cells, row.server))
    .map(({ key, cells, previous }) => ({ key, cells, previous }));
}

/**
 * Which drafted rows may be put back. A row comes back only when the server still holds exactly the value the
 * draft was typed over: someone else's save in between would otherwise be silently overwritten by a stale
 * number — and the 409 that protects a live edit would not fire for a value the person never re-checked.
 *
 * @returns `{ apply: [{ key, cells }], dropped }`
 */
export function decideDraftRestore(draftRows, currentRows) {
  const currentByKey = new Map(currentRows.map((row) => [row.key, row]));
  const apply = [];
  let dropped = 0;
  for (const row of draftRows) {
    const current = currentByKey.get(row.key);
    if (current && sameJson(current.previous, row.previous)) apply.push({ key: row.key, cells: row.cells });
    else dropped += 1;
  }
  return { apply, dropped };
}

function readableStorage(storage) {
  if (storage) return storage;
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage;
  } catch {
    return null;
  }
}

function draftKeys(target) {
  const keys = [];
  if (!target) return keys;
  try {
    for (let index = 0; index < target.length; index += 1) {
      const key = target.key(index);
      if (key?.startsWith(`${KEY_PREFIX}/`)) keys.push(key);
    }
  } catch {
    return [];
  }
  return keys;
}

/** An explicit logout: nothing typed under this session outlives it. Idle logouts do not call this. */
export function clearAllDrafts(storage = null) {
  const target = readableStorage(storage);
  draftKeys(target).forEach((key) => {
    try {
      target.removeItem(key);
    } catch {
      /* best effort */
    }
  });
}

function isDraft(value) {
  return Boolean(value) && value.v === DRAFT_VERSION && value.data !== undefined;
}

/**
 * @param options.identity  () => `{ userId, accountId }` or null while the session is unknown
 * @param options.storage   defaults to sessionStorage; never localStorage — a draft belongs to this tab
 * @param options.doc       document for the notice
 */
export function createDraftController({
  identity,
  storage = null,
  now = () => Date.now(),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  doc = typeof document === 'undefined' ? null : document,
} = {}) {
  const pending = new Map();
  const notices = new WeakMap();

  const store = () => readableStorage(storage);

  function remove(key) {
    try {
      store()?.removeItem(key);
    } catch {
      /* nothing to restore later is the worst case */
    }
  }

  function write(entry) {
    const data = entry.build();
    const key = draftKey(entry.identity, entry.editorId, entry.scope);
    if (data === null || data === undefined) {
      remove(key);
      return;
    }
    try {
      store()?.setItem(key, JSON.stringify({ v: DRAFT_VERSION, savedAt: now(), data }));
    } catch {
      /* quota or blocked storage: the editor works as it did before drafts */
    }
  }

  function settle(editorId) {
    const entry = pending.get(editorId);
    if (!entry) return;
    pending.delete(editorId);
    clearTimer(entry.timer);
    write(entry);
  }

  const controller = {
    /**
     * Remember the editor's state shortly after the last keystroke. `build` runs when the timer fires (or on
     * `flush`), reads the form then, and returns the payload to keep — `null` removes the draft.
     */
    schedule(editorId, scope, build) {
      const who = identity();
      if (!who) return;
      const previous = pending.get(editorId);
      if (previous) clearTimer(previous.timer);
      const entry = { editorId, scope, build, identity: who, timer: null };
      entry.timer = setTimer(() => settle(editorId), DRAFT_DEBOUNCE_MS);
      pending.set(editorId, entry);
    },

    /** Write everything still waiting for its timer: the page is going away (reload, session end). */
    flush() {
      [...pending.keys()].forEach(settle);
    },

    /** Drop a waiting write without storing it: the form it would read has just been redrawn. */
    cancel(editorId) {
      const entry = pending.get(editorId);
      if (!entry) return;
      clearTimer(entry.timer);
      pending.delete(editorId);
    },

    read(editorId, scope) {
      const who = identity();
      if (!who) return null;
      try {
        const raw = store()?.getItem(draftKey(who, editorId, scope));
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        return isDraft(parsed) ? parsed.data : null;
      } catch {
        return null;
      }
    },

    clear(editorId, scope) {
      controller.cancel(editorId);
      const who = identity();
      if (who) remove(draftKey(who, editorId, scope));
    },

    /** A different person is signed in: whatever the previous one left in this tab is not theirs to see. */
    purgeForeign() {
      const who = identity();
      if (!who) return;
      draftKeys(store()).forEach((key) => {
        const parsed = parseDraftKey(key);
        if (!parsed || parsed.userId !== String(who.userId)) remove(key);
      });
    },

    clearAll() {
      pending.forEach((entry) => clearTimer(entry.timer));
      pending.clear();
      clearAllDrafts(storage);
    },

    /**
     * Banner above an editor.
     * @param kind  `restored` | `outdated`
     * @param options.dropped   rows left out of a restore because the server value moved
     * @param options.onDiscard run by the "discard draft" button (restored only)
     */
    showNotice(anchor, kind, { dropped = 0, onDiscard = null } = {}) {
      controller.hideNotice(anchor);
      if (!doc || !anchor?.parentNode) return;
      const element = doc.createElement('div');
      element.className = `draft-notice draft-notice--${kind}`;
      element.setAttribute('role', 'status');
      const text = doc.createElement('span');
      if (kind === 'outdated') text.textContent = t('dash.draftOutdated');
      else if (dropped) text.textContent = t('dash.draftRestoredPartial', { count: dropped });
      else text.textContent = t('dash.draftRestored');
      element.append(text);
      const button = doc.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = kind === 'outdated' ? t('dash.draftDismiss') : t('dash.draftDiscard');
      button.addEventListener('click', () => {
        controller.hideNotice(anchor);
        if (kind === 'restored') onDiscard?.();
      });
      element.append(button);
      anchor.parentNode.insertBefore(element, anchor);
      notices.set(anchor, element);
    },

    hideNotice(anchor) {
      const element = notices.get(anchor);
      if (!element) return;
      element.remove?.();
      notices.delete(anchor);
    },
  };

  return controller;
}

/** Wire the page-level triggers that must not lose a waiting write: a session ending and the tab closing. */
export function bindDraftFlush(controller, target = typeof window === 'undefined' ? null : window) {
  if (!target) return;
  // `logout` is the person leaving on purpose (the other reasons — idle, ended, changed — keep their input).
  target.addEventListener('portal:session-transition', (event) => {
    if (event?.detail?.reason === 'logout') controller.clearAll();
    else controller.flush();
  });
  target.addEventListener('pagehide', () => controller.flush());
  target.document?.addEventListener('visibilitychange', () => {
    if (target.document.visibilityState === 'hidden') controller.flush();
  });
}
