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

/**
 * 字段名 → 界面上叫法
 *
 * Pydantic 的校验错误里 `loc` 用的是模型字段名。直接把它显示给用户，
 * 就会出现 `base_url: String should have at least 8 characters` 这种
 * 既英文又暴露内部命名的提示。
 */
const FIELD_LABELS: Record<string, string> = {
  username: '用户名',
  password: '密码',
  name: '名称',
  base_url: '接口地址',
  api_key: '密钥',
  model_name: '模型名',
  capabilities: '能力标记',
  max_tokens: '最大生成长度',
  temperature: '温度',
  prompt_template: 'Prompt 模板',
  is_grading: '是否批改',
  output_format: '输出格式',
  provider_id: '模型提供商',
  description: '说明',
  title: '标题',
  file: '文件',
  body: '请求内容',
}

/** Pydantic v2 的英文 msg → 中文。只覆盖本项目实际会触发的形态 */
function translateMsg(msg: string): string {
  const min = msg.match(/at least (\d+) character/)
  if (min) return `长度不足，至少需要 ${min[1]} 个字符`
  const max = msg.match(/at most (\d+) character/)
  if (max) return `长度超限，最多 ${max[1]} 个字符`
  if (/missing|field required/i.test(msg)) return '该项必填'
  if (/string_too_short|too_short/i.test(msg)) return '内容太短'
  if (/invalid json object|invalid json/i.test(msg)) return '不是有效的 JSON'
  if (/value is not a valid integer/i.test(msg)) return '请填写整数'
  if (/input should be a valid number/i.test(msg)) return '请填写数字'
  if (/value is not a valid enumeration member|invalid enum/i.test(msg))
    return '取值不在可选范围内'
  return msg
}

/** 校验错误（detail 是数组）→ 一句中文 */
function mapValidation(item: unknown): string | null {
  if (!item || typeof item !== 'object') return null
  const e = item as { loc?: unknown; msg?: unknown }
  const loc = Array.isArray(e.loc) ? e.loc.map(String) : []
  // loc 形如 ['body', 'password']；跳过 body/query 这类位置词，取真正的字段名
  const field = [...loc].reverse().find((p) => p !== 'body' && p !== 'query' && p !== 'path')
  const label = field ? FIELD_LABELS[field] : undefined
  const msg = typeof e.msg === 'string' ? translateMsg(e.msg) : ''
  if (!msg) return null
  return label ? `${label}：${msg}` : msg
}

/** 拿不到后端文案时按状态码兜底，绝不把 axios 的英文原文丢给用户 */
function statusFallback(status?: number): string | null {
  switch (status) {
    case 400:
      return '请求没有被接受，请检查填写内容后重试'
    case 401:
      return '登录已过期或密码错误，请重新登录'
    case 403:
      return '当前账号没有执行该操作的权限'
    case 404:
      return '要操作的对象不存在，可能已被删除'
    case 409:
      return '与已有记录冲突，请刷新后重试'
    case 413:
      return '文件太大，请压缩或拆分后重新上传'
    case 422:
      return '填写内容不符合要求，请检查后重试'
    case 429:
      return '操作过于频繁或已超出今日额度，请稍后再试'
    case 500:
      return '服务器处理时出错，详细信息已记录，请稍后重试'
    default:
      if (status && status >= 500) return '上游服务暂时不可用，请稍后重试'
      return null
  }
}

export function extractErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const data = err.response?.data as { detail?: unknown } | undefined
    if (Array.isArray(data?.detail)) {
      const mapped = mapValidation(data.detail[0])
      if (mapped) return mapped
      const first = data.detail[0] as { msg?: unknown }
      if (typeof first?.msg === 'string') return first.msg
    } else if (typeof data?.detail === 'string' && data.detail) {
      // 后端自己写的中文文案优先，它比这里的通用兜底更具体
      return data.detail
    }
    // 走到这里说明后端没给出可读文案（响应体是 Blob、网关返回 HTML、
    // 或 500 的 detail 为空）——按状态码兜底，而不是把英文原文丢上界面
    const fallback = statusFallback(err.response?.status)
    if (fallback) return fallback
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
