import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import test from 'node:test';

// `script-src 'self'` is only safe while nothing on the site runs inline: every page loads its module from a file,
// no markup carries an event-handler attribute, and the code never builds a script or evaluates a string.
const root = new URL('../', import.meta.url);
const pages = readdirSync(root).filter((name) => name.endsWith('.html'));
const sources = (dir) => readdirSync(new URL(dir, root), { recursive: true })
  .filter((name) => name.endsWith('.js'))
  .map((name) => [`${dir}${name}`, readFileSync(new URL(`${dir}${name}`, root), 'utf8')]);

test('both Vercel configs send script-src self', () => {
  for (const path of ['../vercel.json', 'vercel.json']) {
    const config = JSON.parse(readFileSync(new URL(path, root), 'utf8'));
    const csp = config.headers.flatMap((entry) => entry.headers).find((header) => header.key === 'Content-Security-Policy');
    assert.match(csp.value, /(^|; )script-src 'self'(;|$)/, path);
    assert.doesNotMatch(csp.value, /unsafe-inline|unsafe-eval/, path);
  }
});

test('every page runs only external module scripts and carries no inline handler', () => {
  assert.ok(pages.length >= 10);
  for (const page of pages) {
    const html = readFileSync(new URL(page, root), 'utf8');
    for (const [, attrs, body] of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
      assert.match(attrs, /\bsrc="/, `${page}: inline script`);
      assert.equal(body.trim(), '', `${page}: script with a body`);
    }
    assert.equal(/\son[a-z]+\s*=/i.test(html), false, `${page}: inline event handler`);
    assert.equal(/href\s*=\s*["']javascript:/i.test(html), false, `${page}: javascript: link`);
  }
});

test('no source builds a script, an inline handler or evaluates a string', () => {
  for (const [name, code] of sources('src/')) {
    assert.equal(/createElement\(\s*['"]script['"]/.test(code), false, `${name}: creates a script element`);
    assert.equal(/\bnew Function\(|[^\w.]eval\(/.test(code), false, `${name}: evaluates a string`);
    assert.equal(/\son(click|change|input|submit|load|error|focus|blur|key\w+|mouse\w+)\s*=\s*["'`\\]/.test(code), false, `${name}: inline handler in markup`);
    assert.equal(/set(Timeout|Interval)\(\s*['"`]/.test(code), false, `${name}: string passed to a timer`);
  }
});

test('the re-auth dialog styles come from a stylesheet, not an injected <style>', () => {
  const auth = readFileSync(new URL('src/auth.js', root), 'utf8');
  assert.match(auth, /import '\.\/reauth\.css';/);
  assert.equal(/createElement\(\s*['"]style['"]/.test(auth), false);
});
