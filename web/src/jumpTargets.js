// Pure helpers of the floating jump navigation: which blocks of the visible view it lists and which one is current.
// DOM mounting lives in jumpNav.js, so these stay importable by node tests.

export const JUMP_TARGET_ATTR = 'data-jump-target';
export const MIN_JUMP_TARGETS = 2;

// A target hidden by `hidden`, by an inactive view or by a closed report viewer has no layout box.
const isRendered = (element) => element.getClientRects().length > 0;

/**
 * Jump targets inside `root` in document order. Blocks without a label, a duplicate key and blocks
 * that are not rendered are skipped, so the nav never offers a button that scrolls nowhere.
 */
export function collectJumpTargets(root, isVisible = isRendered) {
  if (!root) return [];
  const seen = new Set();
  const targets = [];
  root.querySelectorAll(`[${JUMP_TARGET_ATTR}]`).forEach((element) => {
    const key = element.getAttribute(JUMP_TARGET_ATTR);
    const label = (element.getAttribute('data-jump-label') || '').trim();
    if (!key || !label || seen.has(key) || !isVisible(element)) return;
    seen.add(key);
    targets.push({ key, label, element });
  });
  return targets;
}

export function shouldShowJumpNav(targets) {
  return targets.length >= MIN_JUMP_TARGETS;
}

/** Equal signatures mean the nav needs no rebuild: re-rendering it on every DOM change would drop hover and focus. */
export function jumpTargetsSignature(targets) {
  return targets.map((target) => `${target.key}\u0000${target.label}`).join('\u0001');
}

/**
 * Index of the block the reader is in: the last one whose top edge has reached `offset` px from the viewport top.
 * At the page bottom the last block wins even when it is too short to ever reach the offset.
 * @param {number[]} tops viewport-relative top edges in document order
 * @returns {number} -1 when there are no targets
 */
export function activeTargetIndex(tops, { offset = 120, atBottom = false } = {}) {
  if (!tops.length) return -1;
  if (atBottom) return tops.length - 1;
  let active = 0;
  tops.forEach((top, index) => {
    if (top <= offset) active = index;
  });
  return active;
}
