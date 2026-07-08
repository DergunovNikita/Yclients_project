import { readdir, readFile } from 'node:fs/promises';
import { basename, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export const FORBIDDEN_BROWSER_AUTH_TOKENS = ['portal_access_token', 'Authorization', 'Bearer', 'access_token'];
export const SOURCE_EXTENSIONS = new Set(['.html', '.js', '.mjs']);
export const SOURCE_EXCLUDED_DIRS = new Set(['dist', 'node_modules', 'tests']);

function fileExtension(path) {
  const index = path.lastIndexOf('.');
  return index === -1 ? '' : path.slice(index);
}

export async function browserSourceFiles(dir, { root = dir, excludedDirs = SOURCE_EXCLUDED_DIRS } = {}) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = new URL(entry.name, `${dir.href.replace(/\/$/, '')}/`);
    const relativeParts = path.pathname.slice(root.pathname.length).split('/').filter(Boolean);
    if (relativeParts.some((part) => excludedDirs.has(part))) {
      continue;
    }
    if (entry.isDirectory()) {
      files.push(...await browserSourceFiles(path, { root, excludedDirs }));
    } else if (entry.isFile() && SOURCE_EXTENSIONS.has(fileExtension(entry.name))) {
      files.push(path);
    }
  }
  return files;
}

function decodeLiteral(value) {
  return value.replace(/\\(['"`\\])/g, '$1');
}

export function stringLiterals(source) {
  const literals = [];
  const pattern = /(['"`])((?:\\.|(?!\1)[\s\S])*)\1/g;
  for (const match of source.matchAll(pattern)) {
    literals.push(decodeLiteral(match[2]));
  }
  return literals;
}

export function collapsedLiteralJoins(source) {
  const collapsed = [];
  const joinPattern = /\[(?<items>[\s\S]*?)\]\s*\.\s*join\s*\(\s*(?<quote>['"`])(?<separator>(?:\\.|(?!\k<quote>)[\s\S])*)\k<quote>\s*\)/g;
  for (const match of source.matchAll(joinPattern)) {
    const separator = decodeLiteral(match.groups.separator);
    collapsed.push(stringLiterals(match.groups.items).join(separator));
  }
  return collapsed;
}

export function forbiddenBrowserAuthTokens(source) {
  const tokens = new Set();
  const candidates = [
    source,
    ...stringLiterals(source),
    ...collapsedLiteralJoins(source),
  ];
  for (const candidate of candidates) {
    for (const token of FORBIDDEN_BROWSER_AUTH_TOKENS) {
      if (candidate.includes(token)) tokens.add(token);
    }
  }
  return [...tokens].sort();
}

export async function scanBrowserAuthTokens(rootUrl, { excludedDirs = SOURCE_EXCLUDED_DIRS } = {}) {
  const offenders = [];
  for (const path of await browserSourceFiles(rootUrl, { root: rootUrl, excludedDirs })) {
    const source = await readFile(path, 'utf8');
    const tokens = forbiddenBrowserAuthTokens(source);
    if (tokens.length) {
      offenders.push(`${path.pathname.slice(rootUrl.pathname.length)}: ${tokens.join(', ')}`);
    }
  }
  return offenders;
}

async function main() {
  const rootPath = resolve(process.argv[2] || '.');
  const rootUrl = pathToFileURL(`${rootPath}/`);
  const excludedDirs = basename(rootPath) === 'dist' ? new Set() : SOURCE_EXCLUDED_DIRS;
  const offenders = await scanBrowserAuthTokens(rootUrl, { excludedDirs });
  if (offenders.length) {
    console.error('Browser auth token leak guard failed:');
    for (const offender of offenders) {
      console.error(`- ${offender}`);
    }
    process.exit(1);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
