import './idleSession.css';
import { t } from './i18n.js';

// One place for the thresholds. Idle time is measured across every tab of the origin: the last
// activity of any tab is shared through localStorage, so a tab left in the background cannot end
// the session of a user who is working in another one.
export const IDLE_PROMPT_AFTER_MS = 2 * 60 * 60 * 1000;
const IDLE_CONFIRM_WINDOW_MS = 5 * 60 * 1000;
export const IDLE_LOGOUT_AFTER_MS = IDLE_PROMPT_AFTER_MS + IDLE_CONFIRM_WINDOW_MS;
const IDLE_ACTIVITY_KEY = 'portal_last_activity';

const NOTE_THROTTLE_MS = 1000;
const STORAGE_WRITE_THROTTLE_MS = 5000;
const TICK_MS = 1000;
const ACTIVITY_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart', 'scroll'];

/**
 * Idle-timeout state machine with an injected clock and storage.
 *
 * Phases: `active` -> `prompt` (idle for promptAfterMs) -> `expired` (idle for logoutAfterMs, terminal).
 * Once the prompt threshold has passed, only `confirm()` counts as presence: a mouse nudge by a
 * passer-by of a laptop that slept for hours must not rescue the session.
 */
export function createIdleMonitor({
  now = () => Date.now(),
  storage = null,
  promptAfterMs = IDLE_PROMPT_AFTER_MS,
  logoutAfterMs = IDLE_LOGOUT_AFTER_MS,
  noteThrottleMs = NOTE_THROTTLE_MS,
  writeThrottleMs = STORAGE_WRITE_THROTTLE_MS,
  onPrompt = () => {},
  onResume = () => {},
  onExpire = () => {},
} = {}) {
  let lastLocal = now();
  let lastNote = 0;
  let lastWrite = 0;
  let phase = 'active';

  const readShared = () => {
    try {
      const value = Number(storage?.getItem(IDLE_ACTIVITY_KEY));
      return Number.isFinite(value) ? value : 0;
    } catch {
      return 0;
    }
  };
  const writeShared = (value) => {
    try {
      storage?.setItem(IDLE_ACTIVITY_KEY, String(value));
    } catch {
      /* the tab's own clock still works without the shared one */
    }
  };
  // Capped at "now": a clock moved backwards must read as activity, not as negative idle time.
  const lastActivity = () => Math.min(now(), Math.max(lastLocal, readShared()));
  const idleFor = () => now() - lastActivity();

  function tick() {
    if (phase === 'expired') return phase;
    const idle = idleFor();
    if (idle >= logoutAfterMs) {
      phase = 'expired';
      onExpire();
    } else if (idle >= promptAfterMs) {
      phase = 'prompt';
      onPrompt(logoutAfterMs - idle);
    } else if (phase === 'prompt') {
      phase = 'active';
      onResume();
    }
    return phase;
  }

  function mark(current) {
    lastLocal = current;
    lastWrite = current;
    writeShared(current);
  }

  function noteActivity() {
    if (phase !== 'active') return;
    const current = now();
    if (current - lastNote < noteThrottleMs) return;
    lastNote = current;
    if (idleFor() >= promptAfterMs) {
      tick();
      return;
    }
    lastLocal = current;
    if (current - lastWrite >= writeThrottleMs) {
      lastWrite = current;
      writeShared(current);
    }
  }

  function confirm() {
    if (phase === 'expired') return false;
    if (idleFor() >= logoutAfterMs) {
      tick();
      return false;
    }
    mark(now());
    if (phase === 'prompt') {
      phase = 'active';
      onResume();
    }
    return true;
  }

  // Opening or reloading a page is the user acting; only a page that stays open can go idle.
  mark(now());

  return { tick, noteActivity, confirm, phase: () => phase };
}

export function formatCountdown(ms) {
  const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function sharedStorage() {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function buildPrompt(doc) {
  const modal = doc.createElement('div');
  modal.className = 'idle-modal';
  modal.hidden = true;
  const dialog = doc.createElement('div');
  dialog.className = 'idle-modal__dialog';
  dialog.setAttribute('role', 'alertdialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'idle-modal-title');
  dialog.setAttribute('aria-describedby', 'idle-modal-text');
  const title = doc.createElement('h2');
  title.id = 'idle-modal-title';
  const text = doc.createElement('p');
  text.id = 'idle-modal-text';
  const button = doc.createElement('button');
  button.type = 'button';
  button.className = 'idle-modal__confirm';
  dialog.append(title, text, button);
  modal.append(dialog);
  doc.body.appendChild(modal);
  return { modal, title, text, button };
}

/**
 * Wire the monitor to the page: activity listeners, a one-second tick and the "still there?" dialog.
 *
 * @param {object} options
 * @param {Function} options.onExpire - called once when the confirmation window ran out
 * @param {Function} [options.onContinue] - called after the user confirmed presence
 * @returns {{stop: Function, monitor: object}}
 */
export function startIdleSession({ onExpire, onContinue = () => {}, win = window, doc = document } = {}) {
  let prompt = null;
  let returnFocus = null;
  let timerId = null;

  const monitor = createIdleMonitor({
    storage: sharedStorage(),
    onPrompt(remainingMs) {
      if (!prompt) {
        prompt = buildPrompt(doc);
        prompt.button.addEventListener('click', onConfirm);
      }
      if (prompt.modal.hidden) {
        returnFocus = doc.activeElement;
        prompt.title.textContent = t('idle.title');
        prompt.button.textContent = t('idle.continue');
        prompt.modal.hidden = false;
        prompt.button.focus();
      }
      prompt.text.textContent = t('idle.text', { time: formatCountdown(remainingMs) });
    },
    onResume() {
      if (!prompt || prompt.modal.hidden) return;
      prompt.modal.hidden = true;
      if (typeof returnFocus?.focus === 'function') returnFocus.focus();
      returnFocus = null;
    },
    onExpire() {
      stop();
      if (prompt) prompt.modal.hidden = true;
      onExpire();
    },
  });

  function onActivity() {
    monitor.noteActivity();
  }

  function onWake() {
    if (doc.visibilityState !== 'hidden') monitor.tick();
  }

  function onConfirm() {
    if (monitor.confirm()) onContinue();
  }

  function trapFocus(event) {
    if (event.key === 'Tab' && prompt && !prompt.modal.hidden) {
      event.preventDefault();
      prompt.button.focus();
    }
  }

  function stop() {
    if (timerId !== null) clearInterval(timerId);
    timerId = null;
    ACTIVITY_EVENTS.forEach((name) => win.removeEventListener?.(name, onActivity, true));
    win.removeEventListener?.('focus', onWake);
    doc.removeEventListener?.('visibilitychange', onWake);
    doc.removeEventListener?.('keydown', trapFocus, true);
    prompt?.button.removeEventListener('click', onConfirm);
  }

  ACTIVITY_EVENTS.forEach((name) => win.addEventListener?.(name, onActivity, { capture: true, passive: true }));
  win.addEventListener?.('focus', onWake);
  doc.addEventListener?.('visibilitychange', onWake);
  doc.addEventListener?.('keydown', trapFocus, true);
  timerId = setInterval(() => monitor.tick(), TICK_MS);
  // A pending interval must not keep a test or a closing process alive.
  timerId?.unref?.();

  return { stop, monitor };
}
