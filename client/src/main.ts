/**
 * 应用入口
 *
 * 【Java 对比】
 * - createApp(App) ≈ SpringApplication.run(Main.class)
 * - app.use(plugin) ≈ 往 ApplicationContext 里注册组件/中间件
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css' // Element Plus 全量样式
import zhCn from 'element-plus/es/locale/lang/zh-cn' // 中文语言包

import './style.css' // 项目自定义全局样式（在 Element Plus 之后引入以覆盖）

import App from './App.vue'
import { router } from './router'

const app = createApp(App)

// Pinia 全局状态管理
app.use(createPinia())

// 路由（含全局守卫）
app.use(router)

// Element Plus 组件库，locale 参数让所有内置文案（分页、日期选择器等）显示中文
app.use(ElementPlus, { locale: zhCn })

// 必须最后 mount：所有插件都装好后再渲染，否则首屏拿不到 store/router
app.mount('#app')
