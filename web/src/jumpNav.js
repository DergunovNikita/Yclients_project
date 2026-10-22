import './jumpNav.css';
import { t } from './i18n.js';
import {
  activeTargetIndex,
  collectJumpTargets,
  effectiveTop,
  mutationsAffectJumpTargets,
  jumpTargetsSignature,
  shouldShowJumpNav,
} from './jumpTargets.js';

// Blocks may be re-rendered at any time (plan table, report charts), so the nav watches the content for changes.
const ACTIVE_OFFSET_PX = 120;
const BOTTOM_TOLERANCE_PX = 4;

/**
 * Mounts one floating nav listing the `[data-jump-target]` blocks of the visible view.
 * @returns {{refresh: () => void}} call `refresh` after switching views
 */
export function mountJumpNav() {
  const nav = document.createElement('nav');
  nav.className = 'jump-nav';
  nav.hidden = true;
  document.body.append(nav);

  let targets = [];
  let signature = '';
  let frame = 0;

  function currentIndex() {
    const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - BOTTOM_TOLERANCE_PX;
    return activeTargetIndex(
      targets.map((target) => effectiveTop(
        target.element.getBoundingClientRect().top,
        Number.parseFloat(getComputedStyle(target.element).scrollMarginTop),
        { offset: ACTIVE_OFFSET_PX },
      )),
      { offset: ACTIVE_OFFSET_PX, atBottom },
    );
  }

  function markActive() {
    const index = currentIndex();
    [...nav.children].forEach((button, position) => {
      const active = position === index;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
      // The bar scrolls sideways on phones: keep the current block's button in sight.
      if (active && nav.scrollWidth > nav.clientWidth) {
        const navRect = nav.getBoundingClientRect();
        const rect = button.getBoundingClientRect();
        if (rect.left < navRect.left) nav.scrollLeft -= navRect.left - rect.left;
        else if (rect.right > navRect.right) nav.scrollLeft += rect.right - navRect.right;
      }
    });
  }

  function jumpTo(target) {
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    target.element.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
  }

  function rebuild() {
    frame = 0;
    targets = collectJumpTargets(document.querySelector('section.view.active'));
    const visible = shouldShowJumpNav(targets);
    nav.hidden = !visible;
    document.body.classList.toggle('jump-nav-visible', visible);
    const nextSignature = visible ? jumpTargetsSignature(targets) : '';
    if (nextSignature !== signature) {
      signature = nextSignature;
      nav.setAttribute('aria-label', t('dash.jumpNavLabel'));
      nav.replaceChildren(...(visible ? targets : []).map((target) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'jump-nav__item';
        button.title = target.label;
        const dot = document.createElement('span');
        dot.className = 'jump-nav__dot';
        dot.setAttribute('aria-hidden', 'true');
        const label = document.createElement('span');
        label.className = 'jump-nav__label';
        label.textContent = target.label;
        button.append(dot, label);
        button.addEventListener('click', () => jumpTo(target));
        return button;
      }));
    }
    if (visible) markActive();
  }

  function refresh() {
    if (!frame) frame = requestAnimationFrame(rebuild);
  }

  let scrollFrame = 0;
  window.addEventListener('scroll', () => {
    if (nav.hidden || scrollFrame) return;
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = 0;
      markActive();
    });
  }, { passive: true });
  window.addEventListener('resize', refresh);
  const main = document.querySelector('main');
  if (main) {
    new MutationObserver((records) => {
      if (mutationsAffectJumpTargets(records)) refresh();
    }).observe(main, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['hidden', 'class', 'data-jump-target'],
    });
  }
  refresh();
  return { refresh };
}
