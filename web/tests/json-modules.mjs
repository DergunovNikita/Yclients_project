// Vite imports the locale files as plain JSON; Node refuses a JSON module unless the
// import spells out `with { type: 'json' }`. Without this hook no test can reach
// src/reports/charts.js, because format.js pulls i18n.js, which pulls the locales.
import { registerHooks } from 'node:module';

registerHooks({
  resolve(specifier, context, nextResolve) {
    const resolved = nextResolve(specifier, context);
    return resolved.url.endsWith('.json')
      ? { ...resolved, importAttributes: { type: 'json' } }
      : resolved;
  },
});
