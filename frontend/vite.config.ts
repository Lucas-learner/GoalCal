import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8501',
      '/_goalcal': 'http://127.0.0.1:8501',
    },
  },
});
