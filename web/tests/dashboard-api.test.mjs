import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

class MemoryStorage {
  constructor() {
    this.store = new Map();
  }

  getItem(key) {
    return this.store.has(key) ? this.store.get(key) : null;
  }

  setItem(key, value) {
    this.store.set(key, String(value));
  }

  removeItem(key) {
    this.store.delete(key);
  }

  key(index) {
    return [...this.store.keys()][index] ?? null;
  }

  get length() {
    return this.store.size;
  }
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function passThroughReauth(url, options, requestFn) {
  return requestFn(url, options);
}

async function loadDashboardApi() {
  globalThis.localStorage = new MemoryStorage();
  localStorage.setItem('portal_account_id', '42');
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { language: 'en-US', languages: ['en-US'] },
  });
  globalThis.window = {
    location: {
      href: '',
      origin: 'https://app.example',
      pathname: '/',
      search: '',
      hash: '',
    },
  };
  globalThis.document = {
    cookie: 'portal_csrf=csrf-dashboard-token',
    documentElement: { lang: 'en' },
  };

  const server = await createServer({
    appType: 'custom',
    logLevel: 'silent',
    root: new URL('..', import.meta.url).pathname,
    server: { middlewareMode: true },
    define: {
      'import.meta.env.VITE_API_BASE': JSON.stringify('http://127.0.0.1:9999'),
      'import.meta.env.VITE_API_KEY': JSON.stringify(''),
    },
  });
  const api = await server.ssrLoadModule('/src/dashboardApi.js');
  return { api, server };
}

test('shared dashboard API handles transport, errors, retries and mutations', async (t) => {
  const { api, server } = await loadDashboardApi();
  t.after(() => server.close());

  await t.test('returns JSON and serializes query parameters', async () => {
    let captured;
    const payload = await api.requestJson('/dashboard/test', {
      params: { company_id: 1, empty: '' },
      slowState: false,
      requestWithReauthImpl: passThroughReauth,
      fetchImpl: async (url, options) => {
        captured = { url, options };
        return jsonResponse({ success: true, data: { ok: true } });
      },
    });

    assert.deepEqual(payload.data, { ok: true });
    assert.equal(captured.url, 'http://127.0.0.1:9999/dashboard/test?company_id=1');
    assert.equal(captured.options.credentials, 'include');
    assert.equal(captured.options.headers['X-Portal-Account-Id'], '42');
    assert.equal(captured.options.headers['Content-Type'], undefined);
  });

  await t.test('preserves structured 503 details', async () => {
    const originalConsoleError = console.error;
    console.error = () => {};
    try {
      await assert.rejects(
        api.requestJson('/dashboard/reports/data', {
          slowState: false,
          requestWithReauthImpl: passThroughReauth,
          fetchImpl: async () => jsonResponse({
            detail: {
              code: 'report_calculation_failed',
              message: 'Повторите расчёт',
              retryable: true,
            },
          }, 503),
        }),
        (error) => (
          error.status === 503
          && error.code === 'report_calculation_failed'
          && error.apiStatus === 'server_error'
          && error.retryable === true
        ),
      );
    } finally {
      console.error = originalConsoleError;
    }
  });

  await t.test('maps external cancellation to a superseded request', async () => {
    const controller = new AbortController();
    const pending = api.requestJson('/dashboard/test', {
      signal: controller.signal,
      slowState: false,
      requestWithReauthImpl: (_url, options) => new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => {
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
        }, { once: true });
      }),
    });
    controller.abort();

    await assert.rejects(pending, (error) => api.isSupersededRequest(error));
  });

  await t.test('maps the internal deadline to a retryable timeout', async () => {
    await assert.rejects(
      api.requestJson('/dashboard/test', {
        timeoutMs: 1,
        slowState: false,
        requestWithReauthImpl: (_url, options) => new Promise((_resolve, reject) => {
          options.signal.addEventListener('abort', () => {
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          }, { once: true });
        }),
      }),
      (error) => error.apiStatus === 'timeout' && error.retryable === true,
    );
  });

  await t.test('does not swallow a timeout while parsing the response body', async () => {
    await assert.rejects(
      api.requestJson('/dashboard/test', {
        timeoutMs: 1,
        slowState: false,
        requestWithReauthImpl: passThroughReauth,
        fetchImpl: async (_url, options) => ({
          ok: true,
          status: 200,
          json: () => new Promise((_resolve, reject) => {
            options.signal.addEventListener('abort', () => {
              reject(Object.assign(new Error('body aborted'), { name: 'AbortError' }));
            }, { once: true });
          }),
        }),
      }),
      (error) => error.apiStatus === 'timeout' && error.retryable === true,
    );
  });

  await t.test('rejects malformed successful JSON as a retryable contract error', async () => {
    await assert.rejects(
      api.requestJson('/dashboard/test', {
        slowState: false,
        requestWithReauthImpl: passThroughReauth,
        fetchImpl: async () => ({
          ok: true,
          status: 200,
          json: async () => { throw new SyntaxError('Unexpected token <'); },
        }),
      }),
      (error) => (
        error.name === 'ResponseContractError'
        && error.status === 200
        && error.apiStatus === 'server_error'
        && error.code === 'invalid_json_response'
        && error.retryable === true
      ),
    );
  });

  await t.test('supports a 401 reauthentication retry through the shared callback', async () => {
    let fetchCount = 0;
    const retryingReauth = async (url, options, requestFn) => {
      let response = await requestFn(url, options);
      if (response.status === 401) {
        response = await requestFn(url, { ...options, __retried: true });
      }
      return response;
    };
    const payload = await api.requestJson('/dashboard/test', {
      slowState: false,
      requestWithReauthImpl: retryingReauth,
      fetchImpl: async () => {
        fetchCount += 1;
        return fetchCount === 1
          ? jsonResponse({ detail: 'expired' }, 401)
          : jsonResponse({ success: true, data: { refreshed: true } });
      },
    });

    assert.equal(fetchCount, 2);
    assert.deepEqual(payload.data, { refreshed: true });
  });

  await t.test('serializes POST and PATCH bodies with CSRF headers', async () => {
    const captured = [];
    const fetchImpl = async (url, options) => {
      captured.push({ url, options });
      return jsonResponse({ success: true });
    };

    await api.postJson('/dashboard/save', { value: 1 }, {
      slowState: false,
      requestWithReauthImpl: passThroughReauth,
      fetchImpl,
    });
    await api.patchJson('/dashboard/save', { value: 2 }, {
      slowState: false,
      requestWithReauthImpl: passThroughReauth,
      fetchImpl,
    });

    assert.deepEqual(captured.map(({ options }) => options.method), ['POST', 'PATCH']);
    assert.deepEqual(captured.map(({ options }) => JSON.parse(options.body)), [{ value: 1 }, { value: 2 }]);
    captured.forEach(({ options }) => {
      assert.equal(options.headers['Content-Type'], 'application/json');
      assert.equal(options.headers['X-CSRF-Token'], 'csrf-dashboard-token');
      assert.equal(options.headers['X-Portal-Account-Id'], '42');
    });
  });

  await t.test('falls back from 127.0.0.1 to localhost after a connection error', async () => {
    const urls = [];
    const payload = await api.requestJson('/dashboard/test', {
      slowState: false,
      requestWithReauthImpl: passThroughReauth,
      fetchImpl: async (url) => {
        urls.push(url);
        if (url.includes('127.0.0.1')) throw new Error('connection refused');
        return jsonResponse({ success: true, data: { fallback: true } });
      },
    });

    assert.deepEqual(urls, [
      'http://127.0.0.1:9999/dashboard/test',
      'http://localhost:9999/dashboard/test',
    ]);
    assert.deepEqual(payload.data, { fallback: true });
  });
});

test('renderReportData hides flagged empty tops but keeps ranking data in a non-default metric', async (t) => {
  globalThis.localStorage = new MemoryStorage();
  const server = await createServer({
    appType: 'custom',
    logLevel: 'silent',
    root: new URL('..', import.meta.url).pathname,
    server: { middlewareMode: true },
  });
  t.after(() => server.close());
  const { renderReportData } = await server.ssrLoadModule('/src/reports/renderers/generic.js');

  const container = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null };
  const charts = { clear() {}, render() {} };

  renderReportData(container, {
    source_status: 'ready',
    cards: [],
    charts: [],
    tables: [
      { id: 'flagged_empty', title: 'Hidden Empty Top', columns: [], rows: [], hide_when_empty: true },
      {
        id: 'ranking_nondefault',
        title: 'Kept Ranking Top',
        columns: [{ key: 'staff', label: 'Staff', format: 'text' }],
        rows: [],
        hide_when_empty: true,
        ranking: {
          default_metric: 'pct',
          options: [{ key: 'qty', label: 'Qty' }, { key: 'pct', label: 'Pct' }],
          rows_by_metric: { qty: [{ staff: 'Master' }], pct: [] },
        },
      },
      { id: 'anchor_empty', title: 'Anchor Top', columns: [], rows: [] },
    ],
  }, charts);

  assert.ok(!container.innerHTML.includes('Hidden Empty Top'), 'flagged empty table is omitted');
  assert.ok(container.innerHTML.includes('Kept Ranking Top'), 'ranking with data only in a non-default metric is kept');
  assert.ok(container.innerHTML.includes('Anchor Top'), 'unflagged empty table still renders as an anchor');
});
