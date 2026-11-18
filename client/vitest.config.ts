/**
 * 前端测试配置（vitest）
 *
 * 与 vite.config.ts 分开的理由：构建入口是 `vue-tsc -b && vite build`，
 * 它是本项目唯一的类型门禁，不该为了跑测试去改它。这里只复用同一条 @ 别名。
 *
 * 范围约定：只测 SFC 之外的纯逻辑（API 层错误提取、token 双写、两个 Pinia store）。
 * 组件渲染测试需要 @vue/test-utils，当前不引入——那会把"轻量"变成一套新体系。
 */
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'happy-dom',
    // 不用全局注入：显式 import 才能被 vue-tsc 的类型门禁覆盖
    globals: false,
    include: ['src/**/*.test.ts'],
  },
})
