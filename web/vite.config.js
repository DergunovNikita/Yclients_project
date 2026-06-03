import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineConfig, loadEnv } from 'vite';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, repoRoot, '');
  const apiKey = process.env.API_KEY || rootEnv.API_KEY || '';
  const apiProxy = {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
    configure(proxy) {
      proxy.on('proxyReq', (proxyReq) => {
        if (apiKey) proxyReq.setHeader('X-API-Key', apiKey);
      });
    },
  };

  return {
    server: {
      port: 5173,
      proxy: {
        '/api': {
          ...apiProxy,
          rewrite: (path) => path.replace(/^\/api\/?/, '/'),
        },
        '/dashboard': apiProxy,
      },
    },
  };
});
