/**
 * Axios 客户端实例 + 全局拦截器
 *
 * 【设计要点】
 * - baseURL='/api/v1'：所有请求都以这个前缀开头，配合 vite proxy 转发到后端
 * - 请求拦截器：自动带上 Authorization: Bearer <token>
 * - 响应拦截器：401 时清 token 跳登录页
 *
 * 【Java 对比】
 * - 类似 Feign Client 的 configuration 或者 RestTemplate 的 Interceptor
 */

import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'

// 简化：直接从 localStorage 读；Pinia 里也有 token 但拦截器不好异步获取 store
// 所以我们约定：登录成功同时写 localStorage 和 store，拦截器读 localStorage
const TOKEN_KEY = 'fileagent_token'

export const http = axios.create({
  baseURL: '/api/v1',
  timeout: 180000, // LLM 调用可能很慢，给 3 分钟
})

// ---------- 请求拦截器 ----------
http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// ---------- 响应拦截器 ----------
http.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    // 401：token 过期或非法 → 清 token 跳登录
    // 但登录接口本身 401 不应该跳（是密码错误），要排除
    const url = error.config?.url ?? ''
    if (error.response?.status === 401 && !url.includes('/auth/login')) {
      localStorage.removeItem(TOKEN_KEY)
      // 用 location.replace 而不是 Vue Router，因为拦截器在 API 层，拿不到 router 实例
      if (window.location.pathname !== '/login') {
        window.location.replace('/login')
      }
    }
    return Promise.reject(error)
  },
)

// ---------- 统一的错误消息提取 ----------
// 后端返回 {detail: "..."} 或 FastAPI 校验错误 {detail: [{msg: "..."}]}
// 前端展示时统一转字符串
export function extractErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const data = err.response?.data as { detail?: unknown } | undefined
    if (typeof data?.detail === 'string') return data.detail
    if (Array.isArray(data?.detail) && data.detail[0]?.msg) {
      return String(data.detail[0].msg)
    }
    return err.message || '网络错误'
  }
  if (err instanceof Error) return err.message
  return String(err)
}

// 保存 token 的工具，让 auth store 和拦截器共用一个 key
export const tokenStorage = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}
