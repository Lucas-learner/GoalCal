import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => ({
  base: '/app/',
  plugins: [react()],
  server: {
    proxy: {
      '/api': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
      '/_goalcal': loadEnv(mode, process.cwd(), 'GOALCAL_').GOALCAL_DEV_BACKEND || 'http://127.0.0.1:8517',
    },
  },
}));
