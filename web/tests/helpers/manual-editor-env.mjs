// Fake page the three manual-entry editors run against, plus the scripted conversation both the original
// main.js code and the extracted module were driven through (the trace of that conversation is the golden file).
import { FakeEditorContainer, FakeElement, hash } from './fake-dom.mjs';

const EDITORS = ['review', 'opz', 'yandex'];

export function makeEnv() {
  const log = [];
  const picker = (id, value = '') => new FakeElement({ id, value });
  const button = (text = '') => new FakeElement({ textContent: text });

  const els = {
    reviewFactEditor: new FakeEditorContainer(),
    reviewFactMeta: new FakeElement(),
    reviewFactSave: button('Save'),
    opzFactEditor: new FakeEditorContainer(),
    opzFactMeta: new FakeElement(),
    opzFactSave: button('Save'),
    yandexPayEditor: new FakeEditorContainer(),
    yandexPayMeta: new FakeElement(),
    yandexPayFutureHint: new FakeElement({ hidden: true }),
    yandexPaySave: button('Save'),
  };
  const filterEls = {
    reviewFacts: {
      month: picker('review-month', '2026-09'),
      branch: picker('review-branch', ''),
      staff: picker('review-staff', ''),
      load: button('Refresh'),
    },
    opzFacts: {
      month: picker('opz-month', '2026-09'),
      branch: picker('opz-branch', ''),
      staff: picker('opz-staff', ''),
      load: button('Refresh'),
    },
    yandexPay: {
      month: picker('yandex-month', '2026-09'),
      branch: picker('yandex-branch', ''),
      load: button('Refresh'),
    },
  };
  const customFilterDropdowns = Object.fromEntries(
    Object.values(filterEls).flatMap((filter) => [filter.branch, filter.staff])
      .filter(Boolean)
      .map((select) => [select.id, { refresh: () => log.push(['dropdownRefresh', select.id]) }]),
  );

  const scripts = new Map();
  const deferred = new Map();
  const env = {
    log,
    els,
    filterEls,
    customFilterDropdowns,
    probe: () => ({}),
    lastRetry: null,
    requestCount: 0,
    stale: new Set(),
    confirmAnswer: true,
    confirmCalls: 0,
  };

  function respond(key) {
    const queue = scripts.get(key);
    if (!queue?.length) throw new Error(`no scripted response for ${key}`);
    const next = queue.shift();
    if (next.hold) {
      return new Promise((resolve, reject) => {
        deferred.set(key, { resolve: (value) => resolve(value), reject });
      });
    }
    if (next.error) {
      throw Object.assign(new Error(next.error.message), { status: next.error.status, apiStatus: next.error.apiStatus });
    }
    return Promise.resolve(next.ok);
  }

  env.script = (key, ...responses) => {
    scripts.set(key, [...(scripts.get(key) || []), ...responses]);
  };
  env.release = (key, response) => {
    const pending = deferred.get(key);
    deferred.delete(key);
    if (response.error) {
      pending.reject(Object.assign(new Error(response.error.message), {
        status: response.error.status,
        apiStatus: response.error.apiStatus,
      }));
    } else {
      pending.resolve(response.ok);
    }
  };

  const plain = (value) => JSON.parse(JSON.stringify(value ?? null));

  env.host = {
    fetchJson: async (path, params, options = {}) => {
      log.push(['fetchJson', path, plain(params), { signal: options.signal ?? null, retry: Boolean(options.retry) }]);
      return respond(`GET ${path}`);
    },
    postJson: async (path, body) => {
      log.push(['postJson', path, plain(body)]);
      return respond(`POST ${path}`);
    },
    setApiState: (text, kind) => log.push(['setApiState', text, kind]),
    showError: (message, options = {}) => {
      env.lastRetry = options.retry || null;
      log.push(['showError', message, { apiStatus: options.apiStatus ?? null, retry: Boolean(options.retry) }]);
    },
    clearError: () => log.push(['clearError']),
    updateFloatingEditorSave: () => log.push(['floating', plain(env.probe())]),
    beginViewRequest: (view) => {
      const id = ++env.requestCount;
      log.push(['beginViewRequest', view, id]);
      return {
        signal: `signal-${id}`,
        isCurrent: () => !env.stale.has(id),
        finish: () => log.push(['requestFinish', id]),
      };
    },
    setFilterLoading: (filter, isLoading) => log.push(['setFilterLoading', filter.load === undefined ? '?' : filter.month.id, isLoading]),
    loadSyncStatus: async () => {
      log.push(['loadSyncStatus']);
    },
    viewsWithData: {
      add: (view) => log.push(['viewsWithData', view]),
    },
    setLoadedApiState: (data, options) => log.push(['setLoadedApiState', data, options]),
    // The real one clears the staff pick, reloads the list, then runs the view's own load.
    refreshStaffForBranch: async (filter, loadView) => {
      log.push(['refreshStaffForBranch', filter.month.id]);
      filter.staff.value = '';
      await loadView();
    },
    // The period refilter (branch and worker lists) is not part of what the editors do themselves.
    refilterForPeriod: async () => {},
    isSupersededRequest: (error) => error?.apiStatus === 'superseded',
    confirmLeave: async () => {
      env.confirmCalls += 1;
      log.push(['confirmLeave']);
      return env.confirmAnswer;
    },
  };

  const valueOf = (input) => [input.dataset.companyId ?? input.dataset.throughCompanyId, input.dataset.staffId ?? null, input.value];
  env.snapshot = (label) => {
    const editor = (container, meta, save, filters, extra = {}) => ({
      meta: meta.textContent,
      save: { disabled: save.disabled, text: save.textContent },
      html: hash(container.innerHTML),
      inputs: container.parsed.inputs.map((input) => [...valueOf(input), input.disabled]),
      filters: Object.fromEntries(Object.entries(filters).filter(([key]) => key !== 'load').map(([key, el]) => [key, el.value])),
      ...extra,
    });
    log.push(['snapshot', label, {
      review: editor(els.reviewFactEditor, els.reviewFactMeta, els.reviewFactSave, filterEls.reviewFacts),
      opz: editor(els.opzFactEditor, els.opzFactMeta, els.opzFactSave, filterEls.opzFacts, {
        totals: els.opzFactEditor.parsed.inputs.map((input) => input.row?.totalCell?.textContent ?? null),
      }),
      yandex: editor(els.yandexPayEditor, els.yandexPayMeta, els.yandexPaySave, filterEls.yandexPay, {
        futureHintHidden: els.yandexPayFutureHint.hidden,
      }),
      state: plain(env.probe()),
    }]);
  };
  // The first render of each editor is also kept readable, so a drift in markup can be read off the diff.
  env.markup = () => ({
    review: els.reviewFactEditor.innerHTML,
    opz: els.opzFactEditor.innerHTML,
    yandex: els.yandexPayEditor.innerHTML,
  });

  env.names = EDITORS;
  return env;
}

export const REVIEW_ROWS = {
  data: {
    total_value: 12,
    rows: [
      {
        company_id: 1, company_title: 'Alpha', staff_id: 10, staff_name: 'Ann', value: 5, is_active: true,
        updated_by_name: 'Bob <b>', updated_at: '2026-09-01T10:00:00',
      },
      {
        company_id: 1, company_title: null, staff_id: 11, staff_name: 'Cy', value: null, is_active: false,
        updated_by_name: null, updated_at: null,
      },
      { company_id: 2, company_title: 'Beta & Co', staff_id: 12, staff_name: 'Di', value: 7, updated_by_name: 'Eve', updated_at: '2026-09-02T23:30:00' },
    ],
  },
};

export const OPZ_ROWS = {
  data: {
    current_total: 40,
    manual_total: 6,
    combined_total: 46,
    rows: [
      {
        company_id: 1, company_title: 'Alpha', staff_id: 10, staff_name: 'Ann', current_value: 25, value: 4, total_value: 29,
        counted: true, is_active: true, updated_by_name: 'Bob', updated_at: '2026-09-01T10:00:00',
      },
      {
        company_id: 1, company_title: 'Alpha', staff_id: 11, staff_name: 'Cy', current_value: 15, value: null, total_value: 15,
        counted: false, is_active: false, updated_by_name: null, updated_at: null,
      },
      { company_id: 2, company_title: null, staff_id: 12, staff_name: 'Di', current_value: 0, value: 2, counted: true },
    ],
  },
};

export const YANDEX_ROWS = {
  data: {
    editable: true,
    rows: [
      {
        company_id: 1, company_title: 'Alpha', cashless_yclients: 120000.5, value: 2000.25, data_through: '2026-09-10',
        default_data_through: '2026-09-29', max_data_through: '2026-09-29', counted: true, updated_by_name: 'Bob',
        updated_at: '2026-09-10T10:00:00',
      },
      {
        company_id: 2, company_title: 'Beta', cashless_yclients: 5000, value: null, data_through: null,
        default_data_through: '2026-09-29', max_data_through: '2026-09-29', counted: false,
      },
    ],
  },
};

export const YANDEX_FUTURE = { data: { editable: false, rows: YANDEX_ROWS.data.rows } };
