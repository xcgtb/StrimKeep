import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// 产物仍然输出到仓库根的 static/，与 1.4.7 的部署方式保持一致：
// 后端 app/main.py 直接托管 static/index.html，镜像不需要 node 只构建一次。
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
