/**
 * 应用入口
 *
 * 【Java 对比】
 * - createApp(App) ≈ SpringApplication.run(Main.class)
 * - app.use(plugin) ≈ 往 ApplicationContext 里注册组件/中间件
 *
 * Element Plus 走按需引入（见 vite.config.ts），所以这里不再 app.use(ElementPlus)：
 * 模板里的 el-* 由 unplugin-vue-components 自动装配并带各自的样式。
 * 剩下三样是那个插件覆盖不到的，必须手工接上：
 *   1) ElMessage / ElMessageBox —— 代码里调用，不是标签，扫不到也就带不到样式
 *   2) v-loading 指令 —— 指令不在组件解析范围内
 *   3) 中文语言包 —— 原来由 app.use 传 locale，现在换成 App.vue 里的 el-config-provider
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { vLoading } from 'element-plus'

// 函数式 API 的样式只能显式引入，漏一个就是"能弹出但没样式"
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/loading/style/css'

import './style.css' // 项目自定义全局样式（在 Element Plus 之后引入以覆盖）
import './assets/theme.css' // 主题层：覆盖 Element Plus CSS 变量（回滚只需删这一行）

import App from './App.vue'
import { router } from './router'

const app = createApp(App)

// Pinia 全局状态管理
app.use(createPinia())

// 路由（含全局守卫）
app.use(router)

// v-loading：表格加载态，三个列表页都在用
app.directive('loading', vLoading)

// 必须最后 mount：所有插件都装好后再渲染，否则首屏拿不到 store/router
app.mount('#app')
