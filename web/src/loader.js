// Loading indicator of a dashboard area: a barber pole while nothing is on screen yet ("first"), a running stripe over
// dimmed content when data is being refreshed ("refresh"). Two layers: createLoaderTiming is a DOM-free state machine
// (importable by node tests with one injected fake timer), mountLoader draws it into a placeholder element.
// The stylesheet (loader.css) is imported by the page, not here, so this module stays loadable in plain Node.

export const SHOW_DELAY_MS = 400;
export const MIN_VISIBLE_MS = 500;
export const CAPTION_INTERVAL_MS = 2700;

export const CAPTION_KEYS = [
  'loader.trim',
  'loader.columns',
  'loader.numbers',
  'loader.contours',
  'loader.tables',
  'loader.styling',
  'loader.totals',
  'loader.shine',
  'loader.touches',
];

/**
 * @param {object} hooks onShow(mode), onHide(), onCaption(index), onModeChange(mode)
 * @param {object} deps setTimer / clearTimer / now, injectable so tests need no real clock
 * @returns {{start(mode: 'first' | 'refresh'): void, stop(): void, isVisible(): boolean}}
 */
export function createLoaderTiming({
  onShow,
  onHide,
  onCaption,
  onModeChange,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
  now = () => performance.now(),
} = {}) {
  let mode = 'first';
  let visible = false;
  let shownAt = 0;
  let captionIndex = 0;
  let showTimer = null;
  let hideTimer = null;
  let captionTimer = null;

  const clear = (timer) => {
    if (timer !== null) clearTimer(timer);
    return null;
  };

  // A recursive timeout rather than setInterval: one timer id at a time, one fake timer in tests.
  function scheduleCaption() {
    captionTimer = setTimer(() => {
      captionIndex = (captionIndex + 1) % CAPTION_KEYS.length;
      onCaption?.(captionIndex);
      scheduleCaption();
    }, CAPTION_INTERVAL_MS);
  }

  function beginCaptions() {
    captionIndex = 0;
    onCaption?.(0);
    captionTimer = clear(captionTimer);
    scheduleCaption();
  }

  function show() {
    showTimer = null;
    visible = true;
    shownAt = now();
    onShow?.(mode);
    if (mode === 'first') beginCaptions();
  }

  function hide() {
    hideTimer = null;
    captionTimer = clear(captionTimer);
    visible = false;
    onHide?.();
  }

  function switchMode(next) {
    if (next === mode) return;
    mode = next;
    if (!visible) return;
    onModeChange?.(mode);
    if (mode === 'first') beginCaptions();
    else captionTimer = clear(captionTimer);
  }

  return {
    start(next = 'first') {
      hideTimer = clear(hideTimer);
      if (visible) {
        switchMode(next);
        // stop() halts the rotation at once; a start inside its min-visible window must resume it.
        if (mode === 'first' && captionTimer === null) scheduleCaption();
      } else if (showTimer !== null) {
        mode = next;
      } else {
        mode = next;
        showTimer = setTimer(show, SHOW_DELAY_MS);
      }
    },
    stop() {
      showTimer = clear(showTimer);
      captionTimer = clear(captionTimer);
      if (!visible || hideTimer !== null) return;
      const remaining = shownAt + MIN_VISIBLE_MS - now();
      if (remaining > 0) hideTimer = setTimer(hide, remaining);
      else hide();
    },
    isVisible: () => visible,
  };
}

/**
 * Wraps a latest-request handle so that ending it stops the loader, but only while it is still the current request:
 * a superseded request finishing late must not switch off the loader its successor just started.
 */
export function stopLoaderWithRequest(request, stop) {
  return {
    ...request,
    finish: () => {
      if (request.isCurrent()) stop();
      request.finish();
    },
  };
}

function element(tag, className, attributes = {}) {
  const node = document.createElement(tag);
  node.className = className;
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
  return node;
}

/**
 * Draws the loader into an existing empty `<div class="view-loader" hidden>` placeholder. The caption text is read
 * through `t()` at every tick, so a locale switch shows from the next caption.
 * @param {HTMLElement | null} el
 * @param {{t: (key: string) => string}} options plus the timing deps of createLoaderTiming
 */
export function mountLoader(el, options) {
  const { t, setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout, ...timingDeps } = options;
  if (!el) return { start() {}, stop() {} };

  const pole = element('div', 'view-loader__pole', { 'aria-hidden': 'true' });
  pole.append(
    element('span', 'view-loader__ball', {}),
    element('span', 'view-loader__cap', {}),
    element('span', 'view-loader__glass', {}),
    element('span', 'view-loader__cap', {}),
  );
  // The caption rotates, so assistive tech must not read it (the viewer is aria-live); the status below is the one
  // announcement.
  const caption = element('p', 'view-loader__caption', { 'aria-hidden': 'true' });
  const stripe = element('div', 'view-loader__stripe', { 'aria-hidden': 'true' });
  const status = element('span', 'view-loader__status', { role: 'status' });
  el.replaceChildren(pole, caption, stripe, status);

  // Some screen readers skip a live region that appears together with its text, so the text follows the un-hide by one
  // tick. No timer outlives stop(): a pending one is cleared there, and the text it would have written goes in at once.
  let statusTimer = null;
  const writeStatus = () => {
    statusTimer = null;
    status.textContent = t('common.loading');
  };
  const dropStatusTimer = () => {
    if (statusTimer !== null) clearTimer(statusTimer);
    statusTimer = null;
  };

  const timing = createLoaderTiming({
    ...timingDeps,
    setTimer,
    clearTimer,
    onShow(mode) {
      el.dataset.mode = mode;
      el.hidden = false;
      statusTimer = setTimer(writeStatus, 0);
    },
    onHide() {
      dropStatusTimer();
      el.hidden = true;
      status.textContent = '';
    },
    onModeChange(mode) {
      el.dataset.mode = mode;
    },
    onCaption(index) {
      caption.textContent = t(CAPTION_KEYS[index]);
      // Restarts the CSS fade-in without a timer.
      caption.classList.remove('is-changing');
      void caption.offsetWidth;
      caption.classList.add('is-changing');
    },
  });

  return {
    ...timing,
    stop() {
      if (statusTimer !== null) {
        dropStatusTimer();
        writeStatus();
      }
      timing.stop();
    },
  };
}
