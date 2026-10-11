// The machine behind every hand-entered editor (reviews, additional OPZ, Yandex Pay): load, edit, save with
// optimistic locking, the unsaved-changes guard and the filter listeners. What differs between the editors —
// markup, request body, what a typed cell means — arrives as `config`; the page's own services arrive as `host`.
import { t } from './i18n.js';

/**
 * @param config.view         view name for request scoping and `viewsWithData`
 * @param config.filter       the pickers: `{ month, branch, load }` plus `staff` where the editor has one
 * @param config.editorEl     container whose `input` events mark the draft dirty
 * @param config.saveButton   inline save button
 * @param config.path         endpoint, GET and POST alike
 * @param config.params       (filters) => GET query
 * @param config.render       (data, loadedFilters) => draws the rows, then calls `editor.accept()`
 * @param config.draftSnapshot () => comparable form of what the cells hold now
 * @param config.request      () => `{ body, filters }`; may throw, which is shown like a failed save
 * @param config.precheck     optional, runs before the saving state starts: a falsy result means the draft was
 *                            refused (and already reported), otherwise it is the `request()` result
 * @param config.conflictMessageKey  what a 409 tells the user
 * @param config.onInput      optional, runs on every `input` event before the dirty check
 * @param config.emptyMonthRestores  a cleared month picker puts the loaded month back instead of loading
 * @param config.refreshStaffOnBranch  a branch change reloads the worker list first
 * @param host  dashboard services: fetchJson, postJson, beginViewRequest, clearError, showError, setApiState,
 *              setFilterLoading, setLoadedApiState, loadSyncStatus, viewsWithData, isSupersededRequest,
 *              updateFloatingEditorSave, refreshStaffForBranch, refilterForPeriod, guard, filterDropdowns
 */
export function createManualEntryEditor(config, host) {
  const { filter } = config;
  const editor = {
    rows: [],
    editable: true,
    dirty: false,
    saving: false,
    loadedFilters: null,
    savedData: null,
    savedSnapshot: '',
  };

  // The pickers as they read now, as opposed to `loadedFilters`: what the rows were fetched or saved for.
  editor.filters = () => ({
    month: filter.month.value,
    branch: filter.branch.value,
    ...(filter.staff ? { staff: filter.staff.value } : {}),
  });

  editor.restoreFilters = () => {
    if (!editor.loadedFilters) return;
    filter.month.value = editor.loadedFilters.month;
    filter.branch.value = editor.loadedFilters.branch;
    if (filter.staff) filter.staff.value = editor.loadedFilters.staff;
    host.filterDropdowns[filter.branch.id]?.refresh();
    if (filter.staff) host.filterDropdowns[filter.staff.id]?.refresh();
  };

  editor.setDirty = (isDirty) => {
    editor.dirty = isDirty;
    config.saveButton.disabled = editor.saving || !editor.rows.length || !editor.editable;
    host.updateFloatingEditorSave();
  };

  editor.updateDirtyFromForm = () => {
    editor.setDirty(config.draftSnapshot() !== editor.savedSnapshot);
  };

  // Called by `config.render` once the rows are on the page: this is the new baseline.
  editor.accept = (data, loadedFilters) => {
    editor.savedData = structuredClone(data || { rows: [] });
    editor.loadedFilters = loadedFilters;
    editor.savedSnapshot = config.draftSnapshot();
    editor.setDirty(false);
  };

  editor.discard = () => {
    editor.restoreFilters();
    if (editor.savedData) config.render(structuredClone(editor.savedData), editor.filters());
    else editor.setDirty(false);
  };

  editor.loadEditor = async ({ signal = null } = {}) => {
    const filters = editor.filters();
    const payload = await host.fetchJson(config.path, config.params(filters), {
      retry: () => editor.load(),
      signal,
    });
    config.render(payload.data, filters);
  };

  editor.load = async () => {
    const request = host.beginViewRequest(config.view);
    host.clearError();
    host.setFilterLoading(filter, true);

    const syncStatus = host.loadSyncStatus();
    try {
      await editor.loadEditor({ signal: request.signal });
      if (!request.isCurrent()) return;
      host.viewsWithData.add(config.view);
      host.clearError();
      host.setLoadedApiState(null, { empty: editor.rows.length === 0 });
      await syncStatus;
    } catch (error) {
      if (host.isSupersededRequest(error) || !request.isCurrent()) return;
      host.showError(error.message, { apiStatus: error.apiStatus, retry: () => editor.load() });
    } finally {
      if (request.isCurrent()) {
        host.setFilterLoading(filter, false);
        request.finish();
      }
    }
  };

  // Someone saved the same month between our load and our save. Their value wins; what we typed
  // is not merged in, because the sum of two people's edits is nobody's intent.
  async function recoverFromConflict() {
    try {
      await editor.loadEditor();
      host.showError(t(config.conflictMessageKey), { apiStatus: 'ready' });
    } catch (reloadError) {
      host.showError(reloadError.message, { apiStatus: reloadError.apiStatus, retry: () => editor.load() });
    }
  }

  editor.save = async () => {
    if (editor.saving || !editor.rows.length || !editor.editable) return;
    let refused = null;
    if (config.precheck) {
      refused = config.precheck();
      if (!refused) return;
    }
    editor.saving = true;
    // A key typed while the request is out would be lost: the response re-renders the rows and takes a new baseline.
    config.editorEl.inert = true;
    host.updateFloatingEditorSave();
    host.clearError();
    config.saveButton.disabled = true;
    config.saveButton.textContent = t('common.saving');
    host.setApiState(t('dash.apiSaving'), 'warn');

    try {
      // The saved response belongs to the scope the rows were loaded for, whatever the pickers show now.
      const { body, filters } = refused || config.request();
      const payload = await host.postJson(config.path, body);
      config.render(payload.data, filters);
      host.setApiState(t('dash.apiConnected'), 'ok');
    } catch (error) {
      if (error.status === 409) await recoverFromConflict();
      else host.showError(error.message, { apiStatus: error.apiStatus, retry: () => editor.save() });
    } finally {
      editor.saving = false;
      config.editorEl.inert = false;
      config.saveButton.textContent = t('dash.saveFact');
      editor.setDirty(editor.dirty);
    }
  };

  // Every picker change goes through the guard; declining puts the pickers back where the rows came from.
  async function guarded(action) {
    const changed = await host.guard.run(action);
    if (!changed) editor.restoreFilters();
  }

  editor.bind = () => {
    filter.load.addEventListener('click', () => host.guard.run(() => editor.load()));
    filter.month.addEventListener('change', async () => {
      const requestedMonth = filter.month.value;
      // A half-typed month has no data to ask for; leave the editor on the month it holds.
      if (config.emptyMonthRestores && !requestedMonth) {
        editor.restoreFilters();
        return;
      }
      await guarded(async () => {
        filter.month.value = requestedMonth;
        // The branch and worker lists depend on the month, and only a month the user kept may rebuild them.
        if ((await host.refilterForPeriod(filter)) === false) return;
        await editor.load();
      });
    });
    filter.branch.addEventListener('change', async () => {
      const requestedBranch = filter.branch.value;
      await guarded(async () => {
        filter.branch.value = requestedBranch;
        host.filterDropdowns[filter.branch.id]?.refresh();
        if (config.refreshStaffOnBranch) await host.refreshStaffForBranch(filter, () => editor.load());
        else await editor.load();
      });
    });
    if (filter.staff) {
      filter.staff.addEventListener('change', async () => {
        const requestedStaff = filter.staff.value;
        await guarded(async () => {
          filter.staff.value = requestedStaff;
          host.filterDropdowns[filter.staff.id]?.refresh();
          await editor.load();
        });
      });
    }
    config.saveButton.addEventListener('click', () => editor.save());
    config.editorEl.addEventListener('input', (event) => {
      config.onInput?.(event);
      editor.updateDirtyFromForm();
    });
  };

  return editor;
}
