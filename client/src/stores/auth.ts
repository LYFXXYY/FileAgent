/**
 * 认证状态管理 —— Pinia store
 *
 * 【Java 对比】
 * - Pinia store ≈ 单例 Bean + @SessionScope 状态
 *   在 Spring 里你可能会用 @Component + ThreadLocal/HttpSession 存当前用户
 *   前端 Pinia 就是这类"跨组件共享的会话级状态"
 *
 * 【setup store vs option store】
 * - 我们用 setup store 语法（Composition API 风格），比 option store 更灵活
 * - state → ref()/reactive()，getter → computed()，action → 普通函数
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { extractErrorMessage, tokenStorage } from '@/api/client'
import { authApi } from '@/api/auth'
import type { UserRole } from '@/api/types'

const ROLE_KEY = 'fileagent_role'
const DEFAULT_KEY = 'fileagent_can_use_default'

export const useAuthStore = defineStore('auth', () => {
  // ---------- State ----------
  // ref() 声明一个响应式数据 —— 改了自动触发依赖它的组件重渲染
  const token = ref<string | null>(tokenStorage.get())
  const username = ref<string | null>(localStorage.getItem('fileagent_username'))
  // 角色与默认模型授权随登录下发（后端 TokenResponse）。localStorage 双写与 token 同套路：
  // 拦截器/路由守卫拿不到 store，只能读 localStorage
  const role = ref<UserRole>((localStorage.getItem(ROLE_KEY) as UserRole) || 'user')
  const canUseDefault = ref(localStorage.getItem(DEFAULT_KEY) === 'true')

  // ---------- Getter ----------
  // computed() 相当于 Java 的 @Cached getter —— 依赖变了才重算
  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => role.value === 'admin')

  // ---------- Actions ----------
  async function login(user: string, password: string): Promise<void> {
    try {
      const resp = await authApi.login(user, password)
      token.value = resp.data.access_token
      username.value = resp.data.username || user
      role.value = resp.data.role
      canUseDefault.value = resp.data.can_use_default
      tokenStorage.set(resp.data.access_token)
      localStorage.setItem('fileagent_username', username.value || user)
      localStorage.setItem(ROLE_KEY, resp.data.role)
      localStorage.setItem(DEFAULT_KEY, String(resp.data.can_use_default))
    } catch (err) {
      throw new Error(extractErrorMessage(err))
    }
  }

  async function register(user: string, password: string): Promise<void> {
    try {
      await authApi.register(user, password)
      // 注册成功后自动登录，用户体验更顺
      await login(user, password)
    } catch (err) {
      throw new Error(extractErrorMessage(err))
    }
  }

  /**
   * 启动时刷新身份快照：role/can_use_default 是登录时冻结在 localStorage 的，
   * 管理员改动后旧登录态看不到。AppLayout 挂载时调一次，变更无需重登即生效。
   * 失败静默（401 由 axios 拦截器统一踢登录页）；这里不能抛，否则启动被一个
   * 可选刷新拖死。
   */
  async function refreshProfile(): Promise<void> {
    if (!token.value) return
    try {
      const resp = await authApi.me()
      role.value = resp.data.role
      canUseDefault.value = resp.data.can_use_default
      username.value = resp.data.username
      localStorage.setItem(ROLE_KEY, resp.data.role)
      localStorage.setItem(DEFAULT_KEY, String(resp.data.can_use_default))
      localStorage.setItem('fileagent_username', resp.data.username)
    } catch {
      // 忽略：拦截器已处理 401，其余错误保持旧快照继续用
    }
  }

  function logout(): void {
    token.value = null
    username.value = null
    role.value = 'user'
    canUseDefault.value = false
    tokenStorage.clear()
    localStorage.removeItem('fileagent_username')
    localStorage.removeItem(ROLE_KEY)
    localStorage.removeItem(DEFAULT_KEY)
  }

  return {
    token,
    username,
    role,
    canUseDefault,
    isLoggedIn,
    isAdmin,
    login,
    register,
    logout,
    refreshProfile,
  }
})
