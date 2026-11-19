/**
 * 规范库 CRUD（阶段 6）
 *
 * 上传会同步做向量编码，所以这份 API 的失败文案里可能出现"向量编码失败"——
 * 那是后端刻意整份回滚，不会留下"文本进了、检索看不见"的半成品。
 */

import { http } from './client'
import type { SpecDoc } from './types'

export const specApi = {
  list() {
    return http.get<SpecDoc[]>('/specs')
  },
  upload(file: File, title: string) {
    const form = new FormData()
    form.append('file', file)
    form.append('title', title)
    // 与 processApi.upload 同一套写法：显式声明 multipart，boundary 由 axios 交给浏览器补
    return http.post<SpecDoc>('/specs/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  remove(id: number) {
    return http.delete(`/specs/${id}`)
  },
}
