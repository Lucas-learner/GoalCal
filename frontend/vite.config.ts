import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => ({
  base: '/app/',
  plugins: [react()],
  build: {
    rollupOptions: {
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
    },
  },
}));
