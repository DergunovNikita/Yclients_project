// Small DOM-free pieces of the admin page, kept apart from admin.js so node can test them.

/**
 * Open/close bookkeeping for the page's modals.
 *
 * `entries` are `{ modal, close }` in Escape priority, top-most first. The page scrolls behind an open modal
 * only when none is open, so the body class follows "is any modal open", not the last one touched.
 */
export function createModalManager(entries, body, openClass = 'admin-modal-open') {
  const anyOpen = () => entries.some(({ modal }) => !modal.hidden);
  return {
    anyOpen,
    open(modal) {
      modal.hidden = false;
      body.classList.add(openClass);
    },
    close(modal) {
      modal.hidden = true;
      if (!anyOpen()) body.classList.remove(openClass);
    },
    /** Closes the top-most open modal; false when none was open. */
    closeTopmost() {
      const top = entries.find(({ modal }) => !modal.hidden);
      if (!top) return false;
      top.close();
      return true;
    },
  };
}

/**
 * Remembers which tenant a request was made for; the returned check says whether it is still the one on screen.
 *
 * A platform admin can switch tenants while the previous tenant's lists are still loading, and an older answer
 * must not overwrite the newer tenant's data.
 */
export function tenantRequestGuard(currentTenantId) {
  const requestedFor = currentTenantId();
  return () => requestedFor === currentTenantId();
}
