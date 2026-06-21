import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineConfig, loadEnv } from 'vite';

const rootDir = fileURLToPath(new URL('.', import.meta.url));
const repoRoot = resolve(dirname(rootDir), '..');

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, repoRoot, '');
  const apiKey = process.env.API_KEY || rootEnv.API_KEY || '';
  const apiTarget = process.env.VITE_DEV_API_TARGET || 'http://127.0.0.1:8000';
  const apiProxy = {
    target: apiTarget,
    changeOrigin: true,
    configure(proxy) {
      proxy.on('proxyReq', (proxyReq) => {
        if (apiKey) proxyReq.setHeader('X-API-Key', apiKey);
      });
    },
  };

  return {
    appType: 'mpa',
    build: {
      rollupOptions: {
        input: {
          main: resolve(rootDir, 'index.html'),
          login: resolve(rootDir, 'login.html'),
          register: resolve(rootDir, 'register.html'),
          forgot: resolve(rootDir, 'forgot-password.html'),
          reset: resolve(rootDir, 'reset-password.html'),
          verify: resolve(rootDir, 'verify-email.html'),
          profile: resolve(rootDir, 'profile.html'),
          admin: resolve(rootDir, 'admin.html'),
        },
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          ...apiProxy,
          rewrite: (path) => path.replace(/^\/api\/?/, '/'),
        },
        '/auth': apiProxy,
        '/dashboard': apiProxy,
      },
    },
  };
});
