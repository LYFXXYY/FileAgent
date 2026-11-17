/**
 * Vue Router 配置 + 全局路由守卫
 *
 * 【Java 对比】
 * - createRouter ≈ Spring MVC 的 DispatcherServlet 注册路径映射
 * - beforeEach ≈ HandlerInterceptor.preHandle()，做鉴权
 *
 * 【路由懒加载】
 * () => import('./views/Xxx.vue') —— Vite 会打包成独立 chunk，进入时才下载
 * 首屏只加载 Login + Layout，其他页面按需加载，类似 Java 的 SPI
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

import AppLayout from '@/components/AppLayout.vue'
import Home from '@/views/Home.vue'

const Login = () => import('@/views/Login.vue')
const Tasks = () => import('@/views/Tasks.vue')
const Providers = () => import('@/views/Providers.vue')
const Logs = () => import('@/views/Logs.vue')
const Admin = () => import('@/views/Admin.vue')

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: Login,
    meta: { public: true }, // 标记：无需登录
  },
  {
    // 主布局：登录后的所有页面都嵌在 AppLayout 里
    path: '/',
    component: AppLayout,
    children: [
      { path: '', name: 'home', component: Home },
      { path: 'tasks', name: 'tasks', component: Tasks },
      { path: 'providers', name: 'providers', component: Providers },
      { path: 'logs', name: 'logs', component: Logs },
      // 管理页：adminOnly 标记 + 守卫兜底；真正的权限把关在后端 require_admin
      { path: 'admin', name: 'admin', component: Admin, meta: { adminOnly: true } },
    ],
  },
  // 未匹配的路径重定向到首页
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

// ---------- 全局前置守卫 ----------
// 类比 Spring Security 的 Filter —— 每个路由跳转前跑一次
router.beforeEach((to) => {
  const auth = useAuthStore()
  // 未登录 + 目标是受限页 → 踢到登录页，并把原目标存到 redirect 参数
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  // 已登录 + 目标又是 login → 直接进首页，不用重复登录
  if (to.name === 'login' && auth.isLoggedIn) {
    return { name: 'home' }
  }
  // 管理页：非 admin 直接回首页。这只是入口拦截，后端 require_admin 才是权限边界
  if (to.meta.adminOnly && !auth.isAdmin) {
    return { name: 'home' }
  }
  return true
})
