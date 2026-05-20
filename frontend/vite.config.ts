import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    target: 'es2020',
    sourcemap: false,
    chunkSizeWarningLimit: 600,
  },
  server: {
    port: 5180,
    proxy: {
      '/api': 'http://127.0.0.1:9100',
    },
  },
});
