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
    // 5173 落在 Windows 保留端口段 5141–5240 内（netsh interface ipv4 show excludedportrange），
    // 永远绑不上，所以这里钉 5241。
    port: 5241,
    // 绑 0.0.0.0：让 `npm run dev` 这条日常命令本身就是"别人也能用"的启动方式，
    // 不用再记 build+preview 两套。代价是同 WiFi 的人都能打到这个端口，
    // 挡住他们的是登录，不是端口——所以对外前先看 server/.env 的配额与注册上限。
    host: true,
    // 占不到 5241 就直接启动失败，而不是静默漂到 5242/5243/…：
    // 漂移过一次，README 与 AGENTS 里的端口就一直和实际不符，且前端代理照常工作、
    // 没人发现，直到下次会话照文档去开 5241 才撞墙。
    strictPort: true,
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

  // 预览构建产物用（`npm run build && npm run preview`）。
  // host: true 与 dev 一致（绑 0.0.0.0），局域网里的别人的机器才连得进来。
  // 代理必须在这里再写一遍：preview 不读 server.proxy，漏了就是"页面打得开、
  // 每个 /api 请求 404"。
  preview: {
    host: true,
    port: 5241,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
