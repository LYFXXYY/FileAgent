/**
 * 后端 API 返回的数据类型（TypeScript 接口）
 *
 * 【为什么手写类型？】
 * - 更严格的做法：后端 OpenAPI schema → 前端 codegen 生成
 * - 学习项目里手写更直观，也帮你理解后端接口的数据形状
 *
 * 【Java 对比】
 * - 类似 client 模块里的 DTO 类，给 controller/service 用
 */

// ========== 认证 ==========
export interface User {
  id: number
  username: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
}

// ========== 提供商 ==========
export interface Provider {
  id: number
  name: string
  base_url: string
  api_key_masked: string
  model_name: string
  max_tokens: number
  temperature: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface ProviderCreatePayload {
  name: string
  base_url: string
  api_key: string
  model_name: string
  max_tokens?: number
  temperature?: number
}

export type ProviderUpdatePayload = Partial<ProviderCreatePayload & { is_active: boolean }>

// ========== 任务 ==========
export interface Task {
  id: number
  user_id: number
  provider_id: number | null
  name: string
  description: string | null
  prompt_template: string
  is_grading: boolean
  created_at: string
  updated_at: string
}

export interface TaskCreatePayload {
  name: string
  description?: string
  prompt_template: string
  is_grading: boolean
  provider_id?: number | null
}

export type TaskUpdatePayload = Partial<TaskCreatePayload>

// ========== 处理流程 ==========
export interface UploadResponse {
  log_id: number
  original_filename: string
  content_length: number
  status: 'pending' | 'processing' | 'done' | 'failed'
  uploaded_at: string
}

export interface ProcessResponse {
  log_id: number
  status: 'pending' | 'processing' | 'done' | 'failed'
  processed_at: string | null
  error_message: string | null
}

// ========== 操作日志 ==========
export interface LogEntry {
  id: number
  user_id: number
  task_id: number | null
  task_name: string
  provider_name: string | null
  original_filename: string
  uploaded_at: string
  processed_at: string | null
  status: 'pending' | 'processing' | 'done' | 'failed'
  error_message: string | null
}

export interface LogListResponse {
  total: number
  page: number
  page_size: number
  items: LogEntry[]
}
