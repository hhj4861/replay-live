import tailwindcss from '@tailwindcss/postcss';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

// A static React build for Vercel. The existing Vinext local development path is preserved.
export default defineConfig({
  plugins: [react()],
  define: {
    __REPLAY_API_BASE__: JSON.stringify(process.env.REPLAY_API_URL ? `${process.env.REPLAY_API_URL.replace(/\/$/, '')}/api` : ''),
    __REPLAY_CLOUD__: JSON.stringify(process.env.REPLAY_CLOUD === '1' && process.env.REPLAY_COMMERCIAL !== '1'),
    __REPLAY_COMMERCIAL__: JSON.stringify(process.env.REPLAY_COMMERCIAL === '1'),
    __REPLAY_LOGIN__: JSON.stringify({ provider: process.env.REPLAY_LOGIN_PROVIDER || 'oidc', google_client_id: process.env.REPLAY_GOOGLE_CLIENT_ID || '' }),
    __REPLAY_OIDC__: JSON.stringify({ authority: process.env.REPLAY_OIDC_AUTHORITY || '', client_id: process.env.REPLAY_OIDC_CLIENT_ID || '', scope: process.env.REPLAY_OIDC_SCOPE || 'openid profile offline_access', audience: process.env.REPLAY_OIDC_AUDIENCE || '' }),
  },
  resolve: { alias: { '@': fileURLToPath(new URL('.', import.meta.url)) } },
  css: { postcss: { plugins: [tailwindcss()] } },
  server: { host: '127.0.0.1', port: 3100, proxy: { '/api': 'http://127.0.0.1:8090' } },
  build: { outDir: 'dist-vercel', emptyOutDir: true },
});
