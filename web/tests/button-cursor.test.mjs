import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import test from 'node:test';

// A disabled control is not "working": the endless-loading cursor is for a button that is busy (`is-loading`).
const srcDir = new URL('../src/', import.meta.url);
const sources = [
  ...readdirSync(srcDir).filter((name) => name.endsWith('.css')).map((name) => [name, new URL(name, srcDir)]),
  ['index.html', new URL('../index.html', import.meta.url)],
].map(([name, url]) => [name, readFileSync(url, 'utf8')]);

function rules(css) {
  return [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => [selector.trim(), body]);
}

test('only a busy button gets the wait/progress cursor, never a disabled one', () => {
  for (const [name, css] of sources) {
    for (const [selector, body] of rules(css)) {
      if (!/cursor:\s*(wait|progress)/.test(body)) continue;
      assert.match(selector, /\.is-loading/, `${name}: "${selector}" sets a busy cursor without being busy-only`);
    }
  }
});

test('a disabled .btn shows not-allowed and a loading one shows progress', () => {
  const auth = rules(sources.find(([name]) => name === 'auth.css')[1]);
  const body = (selector) => auth.find(([sel]) => sel === selector)?.[1] || '';
  assert.match(body('.btn:disabled'), /cursor:\s*not-allowed/);
  assert.match(body('.btn.is-loading:disabled'), /cursor:\s*progress/);
  assert.doesNotMatch(body('.btn.is-loading'), /pointer-events:\s*none/);
});
