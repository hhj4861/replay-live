import { defineConfig, mergeConfig } from 'vite';
import base from './vite.vercel.config';

// Pages serves only the static commercial UI. API and media jobs run separately.
export default mergeConfig(base, defineConfig({
  define: {
    __REPLAY_COMMERCIAL__: 'true',
    __REPLAY_CLOUD__: 'false',
  },
  build: { outDir: 'dist-pages' },
  plugins: [{
    name: 'pages-response-headers',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: '_headers', source: [
        '/*',
        '  X-Content-Type-Options: nosniff',
        '  Referrer-Policy: no-referrer',
        '  X-Frame-Options: DENY',
        '  Permissions-Policy: camera=(), microphone=(), geolocation=()',
        '',
      ].join('\n') });
    },
  }],
}));
