/**
 * Axios 客户端实例 + 全局拦截器
 *
 * 【设计要点】
 * - baseURL='/api/v1'：所有请求都以这个前缀开头，配合 vite proxy 转发到后端
 * - 请求拦截器：自动带上 Authorization: Bearer <token>
 * - 响应拦截器：401 时清 token 跳登录页（保留 redirect 参数，登录后回到原页面）
 * - 自动重试：网络抖动时自动重试 1 次（幂等 GET 请求）
 *
 * 【Java 对比】
 * - 类似 Feign Client 的 configuration 或者 RestTemplate 的 Interceptor
 */

import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'

// 简化：直接从 localStorage 读；Pinia 里也有 token 但拦截器不好异步获取 store
// 所以我们约定：登录成功同时写 localStorage 和 store，拦截器读 localStorage
const TOKEN_KEY = 'fileagent_token'

// 重试配置
const MAX_RETRIES = 1       // 最多重试次数（共 2 次请求）
const RETRY_DELAY = 800     // 重试间隔 ms

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
  // 记录已重试次数（挂在 config 上，响应拦截器可读取）
  if (!(config as { __retryCount?: number }).__retryCount) {
    (config as { __retryCount?: number }).__retryCount = 0
  }
  return config
})

// ---------- 响应拦截器 ----------
http.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as InternalAxiosRequestConfig & { __retryCount?: number }
    const url = config?.url ?? ''

    // 401：token 过期或非法 → 清 token 跳登录
    // 但登录接口本身 401 不应该跳（是密码错误），要排除
    // 同时保留 redirect 参数，让用户登录后能回到当前页面
    if (error.response?.status === 401 && !url.includes('/auth/login')) {
      localStorage.removeItem(TOKEN_KEY)
      // 用 location.replace 而不是 Vue Router，因为拦截器在 API 层，拿不到 router 实例
      // 带 redirect 参数：登录成功后 router guard 会读它跳回原页面
      if (window.location.pathname !== '/login') {
        const redirect = encodeURIComponent(window.location.pathname)
        window.location.replace(`/login?redirect=${redirect}`)
      }
    }

    // 网络错误自动重试（仅 GET 请求 + 未超过最大重试次数）
    // POST/PUT/DELETE 可能有副作用，不自动重试，避免重复操作
    if (
      !error.response &&                  // 无响应 = 网络层错误（断网、超时等）
      config &&
      config.method?.toLowerCase() === 'get' &&  // 只重试幂等 GET
      (config.__retryCount ?? 0) < MAX_RETRIES
    ) {
      config.__retryCount = (config.__retryCount ?? 0) + 1
      // 延迟重试，给网络恢复一点时间
      await new Promise((r) => setTimeout(r, RETRY_DELAY))
      return http(config)
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
