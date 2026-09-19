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

export const useAuthStore = defineStore('auth', () => {
  // ---------- State ----------
  // ref() 声明一个响应式数据 —— 改了自动触发依赖它的组件重渲染
  const token = ref<string | null>(tokenStorage.get())
  const username = ref<string | null>(localStorage.getItem('fileagent_username'))

  // ---------- Getter ----------
  // computed() 相当于 Java 的 @Cached getter —— 依赖变了才重算
  const isLoggedIn = computed(() => !!token.value)

  // ---------- Actions ----------
  async function login(user: string, password: string): Promise<void> {
    try {
      const resp = await authApi.login(user, password)
      token.value = resp.data.access_token
      username.value = user
      tokenStorage.set(resp.data.access_token)
      localStorage.setItem('fileagent_username', user)
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

  function logout(): void {
    token.value = null
    username.value = null
    tokenStorage.clear()
    localStorage.removeItem('fileagent_username')
  }

  return { token, username, isLoggedIn, login, register, logout }
})
