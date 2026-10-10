// Just enough DOM for the manual-entry editors: elements that remember their listeners, and an editor
// container that turns the `<input>` / `<tr>` / total-cell markup it is given into queryable objects.
import { createHash } from 'node:crypto';

export class FakeElement {
  constructor(props = {}) {
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    Object.assign(this, props);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  async dispatch(type, event = {}) {
    for (const listener of this.listeners[type] || []) {
      await listener({ type, target: this, ...event });
    }
  }

  closest() {
    return null;
  }
}

function unescapeAttribute(text) {
  return text
    .replaceAll('&quot;', '"')
    .replaceAll('&#039;', "'")
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&amp;', '&');
}

function parseAttributes(source) {
  const attrs = {};
  for (const match of source.matchAll(/([\w-]+)="([^"]*)"/g)) attrs[match[1]] = unescapeAttribute(match[2]);
  const bare = source.replace(/"[^"]*"/g, '');
  for (const match of bare.matchAll(/\s([\w-]+)(?=\s|$)/g)) attrs[match[1]] ??= '';
  return attrs;
}

const camel = (name) => name.replace(/-(\w)/g, (_, letter) => letter.toUpperCase());

function datasetOf(attrs) {
  return Object.fromEntries(
    Object.entries(attrs).filter(([name]) => name.startsWith('data-')).map(([name, value]) => [camel(name.slice(5)), value]),
  );
}

class FakeInput {
  constructor(attrs, row) {
    this.attrs = attrs;
    this.dataset = datasetOf(attrs);
    this.value = attrs.value ?? '';
    this.disabled = 'disabled' in attrs;
    this.type = attrs.type;
    this.validity = { badInput: false };
    this.row = row;
  }

  closest(selector) {
    if (selector === 'tr') return this.row;
    const match = /^input\[(data-[\w-]+)\]$/.exec(selector);
    return match && match[1] in this.attrs ? this : null;
  }
}

export class FakeEditorContainer extends FakeElement {
  constructor(props) {
    super(props);
    this.parsed = { inputs: [] };
    this.html = '';
  }

  get innerHTML() {
    return this.html;
  }

  set innerHTML(html) {
    this.html = html;
    const inputs = [];
    let row = null;
    const pattern = /<tr\b([^>]*)>|<input\b([\s\S]*?)\/>|<td class="number" data-total-cell>([^<]*)<\/td>/g;
    for (const match of html.matchAll(pattern)) {
      if (match[1] !== undefined) {
        const attrs = parseAttributes(match[1]);
        row = {
          dataset: datasetOf(attrs),
          totalCell: null,
          querySelector(selector) {
            return selector === '[data-total-cell]' ? this.totalCell : null;
          },
        };
      } else if (match[2] !== undefined) {
        inputs.push(new FakeInput(parseAttributes(match[2]), row));
      } else if (row) {
        row.totalCell = { textContent: match[3] };
      }
    }
    this.parsed = { inputs };
  }

  querySelectorAll(selector) {
    const match = /^input\[(data-[\w-]+)\]$/.exec(selector);
    if (!match) throw new Error(`unsupported selector ${selector}`);
    return this.parsed.inputs.filter((input) => match[1] in input.attrs);
  }
}

export const hash = (text) => createHash('sha1').update(text).digest('hex').slice(0, 12);
