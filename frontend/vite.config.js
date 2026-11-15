// Vite 配置:
// - React 插件(@vitejs/plugin-react);
// - 开发期把 /api 代理到本地 FastAPI(localhost:8000),前端代码统一用相对路径 /api/**;
//   preview 阶段同样配代理,便于用 `pnpm exec vite preview` 验证 build 产物;
// - 产物输出到 dist/。
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
});
