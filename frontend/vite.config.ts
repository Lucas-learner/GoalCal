import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

export default defineConfig(({ mode }) => ({
  base: '/app/',
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        app: resolve(__dirname, 'index.html'),
        intro: resolve(__dirname, 'intro.html'),
      },
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('@fullcalendar')) return 'vendor-calendar';
          if (id.includes('/luxon/')) return 'vendor-luxon';
          if (id.includes('@tanstack')) return 'vendor-query';
          if (
            id.includes('/react/') ||
            id.includes('/react-dom/') ||
            id.includes('/react-router') ||
            id.includes('/scheduler/') ||
            id.includes('/@remix-run/')
          ) {
            return 'vendor-react';
          }
          return undefined;
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
      '/_goalcal': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
      '/calendar/feed': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
      '/calendar/webhook': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
      '/favicon.svg': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
      '/favicon.ico': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
      '/apple-touch-icon.png': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
    },
  },
}));
