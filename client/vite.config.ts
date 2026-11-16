import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Vite 配置 —— 类比 Spring Boot 的 application.yml，但用于前端构建工具
export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：模板里用到的 el-* 组件自动 import 并带上自己的样式。
    // 之前是 app.use(ElementPlus) + 全量 CSS，产物一个 893kB 的 chunk。
    // ⚠ 函数式 API（ElMessage / ElMessageBox）和 v-loading 指令**不会被这个插件处理**，
    //   它们在 main.ts 里显式注册并补样式，漏了就是"构建通过、弹窗裸样式"
    Components({
      resolvers: [ElementPlusResolver()],
      // 生成的全局组件类型声明，vue-tsc 靠它认得 el-* 标签
      dts: 'src/components.d.ts',
    }),
  ],

  // 路径别名：@ 引用 src/，避免 "../../.." 相对地狱
  // TypeScript 侧要在 tsconfig.app.json 里加 paths 才能识别
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,
    // 开发代理：/api 前缀的请求转发到后端 8000
    // 【为什么用 proxy？】浏览器直连 http://localhost:8000 会触发 CORS，
    // 虽然我们在后端加了 CORS 中间件解决，但用 proxy 更彻底：
    // 前端所有 fetch 都是同源请求，浏览器压根不检查跨域
    // 生产环境用 Nginx 反代实现同样效果（见 SPEC.md 第 8 节）
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
