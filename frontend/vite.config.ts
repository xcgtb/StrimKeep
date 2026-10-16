import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
export default defineConfig(({ command }) => ({
  plugins: [vue()],
  base: command === 'build' ? '/static/' : '/',
  build: { outDir: '../static', emptyOutDir: true },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.STRIMKEEP_API_TARGET || 'http://127.0.0.1:8321',
        changeOrigin: false,
      },
    },
  },
}));
