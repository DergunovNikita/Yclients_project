import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
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
}

async function loadFormat() {
  // format.js imports i18n.js, which imports the locale JSON files directly (no `type: json`
  // import attribute — Vite applies that transform at bundle time). Plain `node --test` cannot
  // load it as a result, so this loads the real module through Vite's own SSR pipeline instead,
  // the same way dashboard-api.test.mjs does for dashboardApi.js.
  globalThis.localStorage = new MemoryStorage();
  const server = await createServer({
    appType: 'custom',
    logLevel: 'silent',
    root: new URL('..', import.meta.url).pathname,
    server: { middlewareMode: true },
  });
  const format = await server.ssrLoadModule('/src/reports/format.js');
  return { format, server };
}

test('a financials_hidden field renders as an em dash, never a misleading zero', async (t) => {
  const { format, server } = await loadFormat();
  t.after(() => server.close());

  // dashboard_routes.py's _hide_summary_financials pops the whole `revenue` key out of the
  // summary payload for a role without the `revenue` money code — the documented manager
  // default (AGENTS.md, "Недоступный блок убирается, а не показывается с ошибкой"). main.js
  // then reads e.g. revenue.service_revenue off the resulting `{}`, i.e. undefined. Rendering
  // that as "0 ₽" told that manager the branch made no money, which is false — the number is
  // simply not shown to them. reports/format.js already used "—" for this on the Reports page;
  // main.js now shares it (see the comment at its format.js import).
  for (const missing of [undefined, null, '']) {
    assert.equal(format.formatMoney(missing), '—');
    assert.equal(format.formatNumber(missing), '—');
    assert.equal(format.formatDecimal(missing), '—');
  }

  // A real zero is a fact, not an absence, and must stay visibly distinct from "hidden" —
  // the whole point of the guard above.
  assert.equal(format.formatMoney(0), '0 ₽');
  assert.equal(format.formatNumber(0), '0');
  assert.equal(format.formatDecimal(0), '0');
});

test('main.js uses the shared formatters rather than redefining them', async () => {
  // A wiring assertion, and an honest proxy for a call-site test rather than the real
  // thing. main.js exports nothing and calls init() at import, so importing it in node
  // --test would need a full DOM (jsdom), which this project does not carry. What this
  // CAN pin is the regression that actually matters: main.js used to define its own
  // formatMoney/formatNumber/formatDecimal with no null guard, so identical missing data
  // rendered "0 ₽" on the Overview and "—" in Reports. If someone reintroduces a local
  // copy, the drift comes back silently — 52 call sites move together and no payload-level
  // gate can see it, because the difference is in rendering, not in the API response.
  //
  // NOT covered here: whether "—" is the right thing to show at each of those 52 sites.
  // That is a visual judgement. The known-affected group is the extra-service fields,
  // which dashboard_service.py sets to None when the attribution source is not ready.
  const source = await readFile(new URL('../src/main.js', import.meta.url), 'utf8');

  assert.match(
    source,
    /import\s*\{[^}]*\bformatMoney\b[^}]*\}\s*from\s*'\.\/reports\/format\.js'/s,
    'main.js must import the shared formatters from reports/format.js',
  );
  for (const name of ['formatMoney', 'formatNumber', 'formatDecimal']) {
    assert.doesNotMatch(
      source,
      // Declaration AND assignment forms: `const formatMoney = (v) => …` would otherwise
      // slip through. The import assertion above already makes a bare `function` clash a
      // SyntaxError, so the realistic regression is "drop the import, add a local const".
      new RegExp(`^\\s*(?:function\\s+${name}\\s*\\(|(?:const|let|var)\\s+${name}\\s*=)`, 'm'),
      `main.js must not redefine ${name} locally — that is how the "0 ₽" vs "—" drift returned`,
    );
  }
});
