/** 上传 → 处理 → 下载 三步流程 */

import { http } from './client'
import type { ProcessResponse, UploadResponse } from './types'

export const processApi = {
  /**
   * 上传 .docx 文件
   * task_id 是 query 参数（后端这样设计的），file 走 multipart/form-data
   */
  upload(taskId: number, file: File) {
    const form = new FormData()
    form.append('file', file)
    return http.post<UploadResponse>(`/upload?task_id=${taskId}`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  /** 触发 LLM 处理 */
  process(logId: number) {
    return http.post<ProcessResponse>(`/process/${logId}`)
  },

  /** 查询处理状态 —— 只读，不会触发或重复触发 LLM 处理 */
  status(logId: number) {
    return http.get<ProcessResponse>(`/process/${logId}/status`)
  },

  /**
   * 下载结果 .docx
   * 用 responseType: 'blob' 告诉 axios 期望的是二进制文件，不是 JSON
   * 前端拿到后走 URL.createObjectURL 触发浏览器下载
   */
  download(logId: number) {
    return http.get<Blob>(`/download/${logId}`, { responseType: 'blob' })
  },
}
