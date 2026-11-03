import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/benchmaxxing/static',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8123',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (proxyReq, request) => {
            // Development-only proxy: production UI and API share one origin.
            if (request.headers.origin) proxyReq.setHeader('Origin', 'http://127.0.0.1:8123');
          });
        },
      },
    },
  },
});
