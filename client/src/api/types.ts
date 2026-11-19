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
  role: UserRole
  can_use_default: boolean
}

export type UserRole = 'user' | 'admin'

export interface TokenResponse {
  access_token: string
  token_type: string
  username: string
  /** 登录时一并下发；改了角色/授权要重新登录才生效（低频管理操作，可接受） */
  role: UserRole
  can_use_default: boolean
}

// ========== 管理端（/admin/*，仅 admin 可用） ==========
export interface AdminUserRow {
  id: number
  username: string
  role: UserRole
  can_use_default: boolean
  created_at: string
  task_count: number
  log_count: number
  total_tokens: number
}

// ========== 提供商 ==========

/**
 * 模型能力标记。后端存的是逗号分隔串（如 "text,vision"），
 * 处理时按用途筛提供商，避免拿纯文本模型去干视觉活。
 */
export type Capability = 'text' | 'vision' | 'embedding'

export interface Provider {
  id: number
  name: string
  base_url: string
  api_key_masked: string
  model_name: string
  /** 逗号分隔的能力串，用 toCapabilityList() 转成数组再喂给多选框 */
  capabilities: string
  max_tokens: number
  temperature: number
  is_active: boolean
  /** 公共（官方默认）标记：仅 admin 能设置；被授权用户的列表里会出现它 */
  is_public: boolean
  created_at: string
  updated_at: string
}

export interface ProviderCreatePayload {
  name: string
  base_url: string
  api_key: string
  model_name: string
  capabilities?: string
  max_tokens?: number
  temperature?: number
  /** 仅 admin 可设为 true；非 admin 传 true 后端拒 400 */
  is_public?: boolean
}

export type ProviderUpdatePayload = Partial<ProviderCreatePayload & { is_active: boolean }>

// ========== 任务 ==========
// 与后端 schemas/task.py 的 OUTPUT_FORMATS 同步：加值时两处一起改
export type OutputFormat = 'docx' | 'pptx'

export interface Task {
  id: number
  user_id: number
  provider_id: number | null
  name: string
  description: string | null
  prompt_template: string
  is_grading: boolean
  output_format: OutputFormat
  created_at: string
  updated_at: string
}

export interface TaskCreatePayload {
  name: string
  description?: string
  prompt_template: string
  is_grading: boolean
  provider_id?: number | null
  output_format?: OutputFormat
}

export type TaskUpdatePayload = Partial<TaskCreatePayload>

// ========== 处理流程 ==========
export interface UploadResponse {
  log_id: number
  original_filename: string
  content_length: number
  extracted_text: string
  status: 'pending' | 'processing' | 'done' | 'failed'
  uploaded_at: string
}

/**
 * 一条结构化批改意见
 *
 * start / end 是后端在原文里精确匹配出来的偏移，null 表示没匹配上——前端此时显示
 * "未定位"而不是硬找位置，避免把批注指到错的地方。
 */
export interface GradingIssue {
  type: 'typo' | 'grammar' | 'wording' | 'punctuation' | 'structure' | 'logic' | 'content' | 'other'
  original: string
  suggestion: string
  reason: string
  confidence: number
  start: number | null
  end: number | null
}

export interface ChunkProgress {
  /** 切成几块。plan 节点跑完之前后端给 null，此时不显示进度 */
  total: number
  /** 已批完几块 */
  done: number
}

export interface ProcessResponse {
  log_id: number
  status: 'pending' | 'processing' | 'done' | 'failed'
  processed_at: string | null
  error_message: string | null
  processed_content: string | null
  /** null = 非批改任务或历史数据；空数组 = 批改了但没发现问题 */
  issues: GradingIssue[] | null
  /** true = 模型未按 JSON 格式出货，已退回自由文本（和 issues=[] 不是一回事） */
  grading_degraded: boolean
  /**
   * 本次真正进过 prompt 的规范条款（阶段 6）。
   * null = 非批改任务或历史数据；空数组 = 没用规范库（没建库 / 开关关掉 / 没召回相关条款）
   */
  spec_refs?: SpecRef[] | null
  /** 规范检索被降级的原因。与"没查到相关条款"分开显示，否则用户以为库里没内容 */
  spec_note?: string | null
  /** 长文档分块进度；短文、非批改任务、以及还没切块时为 null */
  progress?: ChunkProgress | null
}

/** 一条被引用的规范条款 */
export interface SpecRef {
  /** 条款号，如 2.1.1；解析不出编号时后端回落到 doc3-#7 形态的定位符 */
  clause: string
  score: number
  /** 这一条靠哪一路召回：vector=余弦、lexical=字二元组重合、both=两路都命中。
      两个标尺不同（余弦常见 0.4–0.6，Dice 常见 0.05–0.2），不标来源数字没法解释 */
  via?: 'vector' | 'lexical' | 'both'
  doc_id: number
  seq: number
}

// ========== 规范库（阶段 6） ==========
export interface SpecDoc {
  id: number
  title: string
  source_format: string
  char_count: number
  chunk_count: number
  uploaded_at: string
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
  issue_count: number
}

export interface LogListResponse {
  total: number
  page: number
  page_size: number
  items: LogEntry[]
}
