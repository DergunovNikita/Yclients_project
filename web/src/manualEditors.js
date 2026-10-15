// The three hand-entry editors of the dashboard — reviews, additional OPZ, Yandex Pay — wired to the page.
// manualEntryEditor.js is the machine; this file is what makes each one itself: its markup, what a typed
// cell means, and the request it sends.
import { escapeHtml } from './html.js';
import { intlLocale, t } from './i18n.js';
import { createManualEntryEditor } from './manualEntryEditor.js';
import {
  buildManualFactItems,
  formatInputNumber,
  manualFactRowKey,
  manualFactSaveScope,
  storedValue,
} from './manualFactInput.js';
import { formatMoneyExact, formatNumber } from './reports/format.js';
import { BRANCH_TIME_ZONE, parseServerInstant } from './timestamps.js';
import {
  UNREADABLE_DATE,
  buildYandexPayItems,
  syncYandexPayDate,
  yandexPayDateKey,
  yandexPayDraftKey,
  yandexPayMonthStart,
  yandexPayTotal,
} from './yandexPayInput.js';

function manualFactSavedOn(value) {
  const instant = parseServerInstant(value);
  if (instant === null) return '';
  // Branch time, like every other business timestamp — the viewer's own zone would make the
  // same instant read as two different dates on two screens of the same dashboard.
  return new Intl.DateTimeFormat(intlLocale(), { timeZone: BRANCH_TIME_ZONE }).format(instant);
}

// Two people may edit one row, so the row says which of them touched it last. Values saved
// before the author column existed have a date and no name — keep the half we do have.
function manualFactAuthorCell(row) {
  const parts = [row.updated_by_name ? escapeHtml(row.updated_by_name) : '—'];
  const savedOn = manualFactSavedOn(row.updated_at);
  if (savedOn) parts.push(escapeHtml(savedOn));
  return parts.join(' · ');
}

function manualFactDraftSnapshot(editorEl) {
  return JSON.stringify([...editorEl.querySelectorAll('input[data-staff-id]')].map((input) => ({
    companyId: input.dataset.companyId,
    staffId: input.dataset.staffId,
    value: input.value.trim(),
  })));
}

// Every rendered row goes out with the value the last response showed for it, so the server
// can tell an edit from a row someone else changed since this editor loaded.
function manualFactPayload(loadedFilters, liveFilters, editorEl, rows, invalidMessageKey) {
  const rawByKey = new Map([...editorEl.querySelectorAll('input[data-staff-id]')].map((input) => [
    manualFactRowKey(input.dataset.companyId, input.dataset.staffId),
    input.value,
  ]));
  const built = buildManualFactItems(rows, rawByKey);
  if (built.error) throw new Error(t(invalidMessageKey));
  return { ...manualFactSaveScope(loadedFilters, liveFilters), items: built.items };
}

// Draft model of the reviews / OPZ cells: the typed text against what the server drew, and the stored value the
// text was typed over (what the save would send as `previous_value`).
function manualFactDraftRows(editorEl, rows) {
  const byKey = new Map(rows.map((row) => [manualFactRowKey(row.company_id, row.staff_id), row]));
  return [...editorEl.querySelectorAll('input[data-staff-id]')].map((input) => {
    const key = manualFactRowKey(input.dataset.companyId, input.dataset.staffId);
    const row = byKey.get(key);
    return {
      key,
      cells: { value: input.value.trim() },
      server: { value: formatInputNumber(row?.value) },
      previous: { value: row ? storedValue(row) : null },
    };
  });
}

function applyManualFactDraft(editorEl, items) {
  const cellsByKey = new Map(items.map((item) => [item.key, item.cells]));
  editorEl.querySelectorAll('input[data-staff-id]').forEach((input) => {
    const cells = cellsByKey.get(manualFactRowKey(input.dataset.companyId, input.dataset.staffId));
    if (cells && typeof cells.value === 'string') input.value = cells.value;
  });
}

const manualFactParams = (filters) => ({
  month: filters.month,
  company_id: filters.branch,
  staff_id: filters.staff,
});

function branchTitle(row) {
  return row.company_title || t('dash.branchFallbackWithId', { id: row.company_id });
}

// The cell the person types into; reviews and OPZ share it.
function manualFactInputCell(row) {
  return `
                    <td class="number">
                      <input
                        type="number"
                        min="0"
                        step="1"
                        inputmode="numeric"
                        data-company-id="${escapeHtml(row.company_id)}"
                        data-staff-id="${escapeHtml(row.staff_id)}"
                        value="${escapeHtml(formatInputNumber(row.value))}"
                      />
                    </td>`;
}

function reviewFactTableHtml(rows) {
  return `
      <div class="table-scroll manual-fact-scroll">
        <table class="manual-fact-table">
          <thead>
            <tr>
              <th>${t('dash.branch')}</th>
              <th>${t('dash.administrator')}</th>
              <th class="number">${t('dash.reviewsFact')}</th>
              <th>${t('dash.manualFactAuthor')}</th>
            </tr>
          </thead>
          <tbody>
            ${rows
              .map((row) => {
                const inactiveMark = row.is_active === false
                  ? ` <span class="meta">· ${escapeHtml(t('dash.reviewFactInactive'))}</span>`
                  : '';
                return `
                  <tr>
                    <td>${escapeHtml(branchTitle(row))}</td>
                    <td>${escapeHtml(row.staff_name)}${inactiveMark}</td>${manualFactInputCell(row)}
                    <td class="meta">${manualFactAuthorCell(row)}</td>
                  </tr>
                `;
              })
              .join('')}
          </tbody>
        </table>
      </div>
    `;
}

function opzFactTableHtml(rows) {
  return `
      <div class="table-scroll manual-fact-scroll">
        <table class="manual-fact-table">
          <thead>
            <tr>
              <th>${t('dash.branch')}</th>
              <th>${t('dash.administrator')}</th>
              <th class="number">${t('dash.currentOpz')}</th>
              <th class="number">${t('dash.additionalOpz')}</th>
              <th class="number">${t('dash.opzFactTotal')}</th>
              <th>${t('dash.manualFactAuthor')}</th>
            </tr>
          </thead>
          <tbody>
            ${rows
              .map((row) => {
                const marks = [];
                if (row.is_active === false) marks.push(t('dash.opzFactInactive'));
                if (row.counted === false) marks.push(t('dash.opzFactNotCounted'));
                const inactiveMark = marks.length
                  ? ` <span class="meta">· ${escapeHtml(marks.join(' · '))}</span>`
                  : '';
                const current = Number(row.current_value || 0);
                return `
                  <tr data-current-value="${escapeHtml(current)}" data-counted="${row.counted === false ? 'false' : 'true'}">
                    <td>${escapeHtml(branchTitle(row))}</td>
                    <td>${escapeHtml(row.staff_name)}${inactiveMark}</td>
                    <td class="number">${escapeHtml(formatNumber(current))}</td>${manualFactInputCell(row)}
                    <td class="number" data-total-cell>${escapeHtml(formatNumber(row.total_value ?? current))}</td>
                    <td class="meta">${manualFactAuthorCell(row)}</td>
                  </tr>
                `;
              })
              .join('')}
          </tbody>
        </table>
      </div>
    `;
}

function yandexPayTableHtml(rows, { editable, monthStart }) {
  return `
      <div class="table-scroll manual-fact-scroll">
        <table class="manual-fact-table yandex-pay-table">
          <thead>
            <tr>
              <th>${t('dash.branch')}</th>
              <th class="number">${t('dash.yandexPayCashless')}</th>
              <th class="number">${t('dash.yandexPayAmount')}</th>
              <th>${t('dash.yandexPayDataThrough')}</th>
              <th>${t('dash.manualFactAuthor')}</th>
            </tr>
          </thead>
          <tbody>
            ${rows
              .map((row) => {
                const counted = row.counted !== false;
                const title = branchTitle(row);
                const mark = counted
                  ? ''
                  : ` <span class="meta">· ${escapeHtml(t('dash.yandexPayNotCounted'))}</span>`;
                return `
                  <tr class="${counted ? '' : 'is-muted'}">
                    <td>${escapeHtml(title)}${mark}</td>
                    <td class="number">${escapeHtml(formatMoneyExact(row.cashless_yclients))}</td>
                    <td class="number">
                      <input
                        type="text"
                        inputmode="decimal"
                        autocomplete="off"
                        data-company-id="${escapeHtml(row.company_id)}"
                        value="${escapeHtml(formatInputNumber(row.value))}"
                        ${editable ? '' : 'disabled'}
                      />
                    </td>
                    <td>
                      <input
                        type="date"
                        autocomplete="off"
                        data-through-company-id="${escapeHtml(row.company_id)}"
                        aria-label="${escapeHtml(t('dash.yandexPayDataThrough'))}: ${escapeHtml(title)}"
                        value="${escapeHtml(row.data_through || '')}"
                        ${monthStart ? `min="${escapeHtml(monthStart)}"` : ''}
                        ${row.max_data_through ? `max="${escapeHtml(row.max_data_through)}"` : ''}
                        ${editable ? '' : 'disabled'}
                      />
                    </td>
                    <td class="meta">${manualFactAuthorCell(row)}</td>
                  </tr>
                `;
              })
              .join('')}
          </tbody>
        </table>
      </div>
    `;
}

function yandexPayInvalidMessage(error, row) {
  const branch = branchTitle(row);
  const keys = {
    range: 'dash.yandexPayInvalidRange',
    precision: 'dash.yandexPayInvalidPrecision',
    dateRequired: 'dash.yandexPayInvalidDateRequired',
    dateFormat: 'dash.yandexPayInvalidDateFormat',
    dateRange: 'dash.yandexPayInvalidDateRange',
  };
  return t(keys[error] || 'dash.yandexPayInvalidFormat', { branch });
}

/**
 * Builds the three editors and registers their listeners.
 *
 * @param els       the page elements the editors draw into (editor, meta line, save button per editor)
 * @param filterEls the pickers: `reviewFacts`, `opzFacts`, `yandexPay`
 * @param host      the dashboard services described in manualEntryEditor.js
 */
export function createManualEditors({ els, filterEls, host }) {
  const emptyRows = (editor) => editor.rows.length === 0;

  // ---- reviews ----
  const review = createManualEntryEditor({
    view: 'reviewFacts',
    filter: filterEls.reviewFacts,
    editorEl: els.reviewFactEditor,
    saveButton: els.reviewFactSave,
    path: '/dashboard/plan/reviews_fact',
    params: manualFactParams,
    conflictMessageKey: 'dash.manualFactConflict',
    refreshStaffOnBranch: true,
    draftSnapshot: () => manualFactDraftSnapshot(els.reviewFactEditor),
    draft: {
      id: 'reviewFacts',
      collect: () => manualFactDraftRows(els.reviewFactEditor, review.rows),
      apply: (items) => applyManualFactDraft(els.reviewFactEditor, items),
    },
    request: () => ({
      filters: review.loadedFilters || review.filters(),
      body: manualFactPayload(
        review.loadedFilters, review.filters(), els.reviewFactEditor, review.rows, 'dash.reviewFactNonNegative',
      ),
    }),
    // `loadedFilters` is what `data` was fetched or saved for — the pickers may already show another scope.
    render(data, loadedFilters) {
      review.rows = data?.rows || [];
      const totalValue = data?.total_value || 0;
      els.reviewFactMeta.textContent = t('dash.reviewFactMeta', {
        admins: review.rows.length,
        reviews: formatNumber(totalValue),
      });
      els.reviewFactEditor.innerHTML = emptyRows(review)
        ? `<div class="empty compact">${t('dash.noActiveAdministrators')}</div>`
        : reviewFactTableHtml(review.rows);
      review.accept(data, loadedFilters);
    },
  }, host);

  // ---- additional OPZ ----
  const opzMeta = (current, additional, total) => t('dash.opzFactMeta', {
    admins: opz.rows.length,
    current: formatNumber(current),
    additional: formatNumber(additional),
    total: formatNumber(total),
  });

  // The fact is the sum, so the row shows it before saving — not only after the reload.
  function refreshOpzFactTotals() {
    let currentTotal = 0;
    let additionalTotal = 0;
    els.opzFactEditor.querySelectorAll('input[data-staff-id]').forEach((input) => {
      const row = input.closest('tr');
      if (!row) return;
      const current = Number(row.dataset.currentValue || 0);
      const rawValue = input.value.trim().replace(',', '.');
      const parsed = rawValue === '' ? 0 : Number(rawValue);
      const valid = Number.isFinite(parsed) && parsed >= 0;
      // The server stores whole units, so the preview must not promise a fraction — and a
      // row the reports ignore must not add itself to the totals either.
      const counted = row.dataset.counted !== 'false';
      const additional = valid && counted ? Math.round(parsed) : 0;
      const totalCell = row.querySelector('[data-total-cell]');
      if (totalCell) totalCell.textContent = formatNumber(current + additional);
      currentTotal += current;
      additionalTotal += additional;
    });
    els.opzFactMeta.textContent = opzMeta(currentTotal, additionalTotal, currentTotal + additionalTotal);
  }

  const opz = createManualEntryEditor({
    view: 'opzFacts',
    filter: filterEls.opzFacts,
    editorEl: els.opzFactEditor,
    saveButton: els.opzFactSave,
    path: '/dashboard/plan/opz_fact',
    params: manualFactParams,
    conflictMessageKey: 'dash.manualFactConflict',
    refreshStaffOnBranch: true,
    draftSnapshot: () => manualFactDraftSnapshot(els.opzFactEditor),
    draft: {
      id: 'opzFacts',
      collect: () => manualFactDraftRows(els.opzFactEditor, opz.rows),
      apply: (items) => {
        applyManualFactDraft(els.opzFactEditor, items);
        refreshOpzFactTotals();
      },
    },
    onInput: refreshOpzFactTotals,
    request: () => ({
      filters: opz.loadedFilters || opz.filters(),
      body: manualFactPayload(
        opz.loadedFilters, opz.filters(), els.opzFactEditor, opz.rows, 'dash.opzFactNonNegative',
      ),
    }),
    render(data, loadedFilters) {
      opz.rows = data?.rows || [];
      els.opzFactMeta.textContent = opzMeta(
        data?.current_total || 0,
        data?.manual_total || 0,
        data?.combined_total || 0,
      );
      els.opzFactEditor.innerHTML = emptyRows(opz)
        ? `<div class="empty compact">${t('dash.noActiveAdministrators')}</div>`
        : opzFactTableHtml(opz.rows);
      opz.accept(data, loadedFilters);
    },
  }, host);

  // ---- Yandex Pay ----
  const yandexInputs = () => [...els.yandexPayEditor.querySelectorAll('input[data-company-id]')];
  const yandexRawValues = () => new Map(yandexInputs().map((input) => [input.dataset.companyId, input.value]));
  const yandexDateInputs = () => [...els.yandexPayEditor.querySelectorAll('input[data-through-company-id]')];
  const yandexRawDates = () => new Map(yandexDateInputs().map((input) => [
    input.dataset.throughCompanyId,
    input.validity?.badInput ? UNREADABLE_DATE : input.value,
  ]));

  // The total is what the reports will count once saved, so it follows the cells while typing.
  function refreshYandexPayTotal() {
    els.yandexPayMeta.textContent = t('dash.yandexPayMeta', {
      branches: yandex.rows.length,
      total: formatMoneyExact(yandexPayTotal(yandex.rows, yandexRawValues())),
    });
  }

  // Typing an amount into an empty row proposes the date the server would pick anyway, so the
  // manager sees it before saving; emptying the amount empties the date, as no value has no date.
  function fillYandexPayDate(amountInput) {
    const id = amountInput.dataset.companyId;
    const dateInput = yandexDateInputs().find((input) => input.dataset.throughCompanyId === id);
    if (!dateInput || dateInput.validity?.badInput) return;
    const row = yandex.rows.find((item) => String(item.company_id) === id);
    dateInput.value = syncYandexPayDate(amountInput.value, dateInput.value, row?.default_data_through);
  }

  // The pair the server holds for a branch is (amount, date); the draft is valid only against that same pair.
  function yandexDraftRows() {
    const dateInputs = new Map(yandexDateInputs().map((input) => [input.dataset.throughCompanyId, input]));
    const rowsById = new Map(yandex.rows.map((row) => [String(row.company_id), row]));
    return yandexInputs().map((input) => {
      const id = input.dataset.companyId;
      const row = rowsById.get(id);
      return {
        key: id,
        cells: { value: input.value.trim(), date: dateInputs.get(id)?.value || '' },
        server: { value: formatInputNumber(row?.value), date: row?.data_through || '' },
        previous: { value: row ? storedValue(row) : null, data_through: row?.data_through ?? null },
      };
    });
  }

  function applyYandexDraft(items) {
    const cellsByKey = new Map(items.map((item) => [item.key, item.cells]));
    yandexInputs().forEach((input) => {
      const cells = cellsByKey.get(input.dataset.companyId);
      if (cells && typeof cells.value === 'string') input.value = cells.value;
    });
    yandexDateInputs().forEach((input) => {
      const cells = cellsByKey.get(input.dataset.throughCompanyId);
      if (cells && typeof cells.date === 'string') input.value = cells.date;
    });
    refreshYandexPayTotal();
  }

  const yandex = createManualEntryEditor({
    view: 'yandexPayFacts',
    filter: filterEls.yandexPay,
    editorEl: els.yandexPayEditor,
    saveButton: els.yandexPaySave,
    path: '/dashboard/payments/yandex_pay',
    params: (filters) => ({ month: filters.month, company_id: filters.branch }),
    conflictMessageKey: 'dash.yandexPayConflict',
    emptyMonthRestores: true,
    draft: { id: 'yandexPayFacts', collect: yandexDraftRows, apply: applyYandexDraft },
    draftSnapshot: () => {
      const dates = yandexRawDates();
      return JSON.stringify(yandexInputs().map((input) => [
        input.dataset.companyId,
        yandexPayDraftKey(input.value),
        yandexPayDateKey(dates.get(input.dataset.companyId)),
      ]));
    },
    onInput: (event) => {
      const amount = event.target.closest?.('input[data-company-id]');
      if (amount) fillYandexPayDate(amount);
      refreshYandexPayTotal();
    },
    // The month the rows were loaded for, not the picker: it may have moved without a reload.
    precheck: () => {
      const filters = yandex.loadedFilters || yandex.filters();
      const built = buildYandexPayItems(yandex.rows, yandexRawValues(), yandexRawDates(), filters.month);
      if (built.error) {
        host.showError(yandexPayInvalidMessage(built.error, built.row), { apiStatus: 'ready' });
        return null;
      }
      return {
        filters,
        body: {
          month: filters.month,
          company_id: filters.branch ? Number(filters.branch) : null,
          items: built.items,
        },
      };
    },
    render(data, loadedFilters) {
      yandex.rows = data?.rows || [];
      yandex.editable = data?.editable !== false;
      els.yandexPayFutureHint.hidden = yandex.editable;
      const monthStart = yandexPayMonthStart(loadedFilters.month);
      els.yandexPayEditor.innerHTML = emptyRows(yandex)
        ? `<div class="empty compact">${t('dash.yandexPayNoBranches')}</div>`
        : yandexPayTableHtml(yandex.rows, { editable: yandex.editable, monthStart });
      refreshYandexPayTotal();
      yandex.accept(data, loadedFilters);
    },
  }, host);

  [review, opz, yandex].forEach((editor) => editor.bind());
  return {
    review,
    opz,
    yandex,
    byView: { reviewFacts: review, opzFacts: opz, yandexPayFacts: yandex },
  };
}
