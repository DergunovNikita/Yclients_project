const apiBase = import.meta.env.VITE_API_BASE || '';

function apiUrl(path, params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      qs.set(key, value);
    }
  });
  const suffix = qs.toString() ? `?${qs}` : '';
  const normalizedPath = `${path}${suffix}`;
  if (!apiBase) return normalizedPath;
  return `${apiBase.replace(/\/$/, '')}${normalizedPath}`;
}

function apiUrlCandidates(path, params = {}) {
  const primary = apiUrl(path, params);
  const candidates = [primary];
  if (apiBase.includes('127.0.0.1')) {
    candidates.push(primary.replace('127.0.0.1', 'localhost'));
  } else if (apiBase.includes('localhost')) {
    candidates.push(primary.replace('localhost', '127.0.0.1'));
  }
  return [...new Set(candidates)];
}

export async function fetchJson(path, params = {}) {
  const errors = [];
  for (const url of apiUrlCandidates(path, params)) {
    let response;
    try {
      response = await fetch(url);
    } catch (error) {
      errors.push(`${url}\n${error.message}`);
      continue;
    }

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`API вернул ${response.status} для ${url}\n\n${body.slice(0, 1000)}`);
    }

    const payload = await response.json();
    if (payload.success === false) {
      throw new Error(`API вернул success=false для ${url}`);
    }
    return payload;
  }

  throw new Error(`Не удалось подключиться к API.\n\n${errors.join('\n\n')}`);
}
