/** 操作日志查询 */

import { http } from './client'
import type { LogListResponse } from './types'

export const logsApi = {
  list(page: number, pageSize: number, status?: string, filename?: string) {
    return http.get<LogListResponse>('/logs', {
      params: { page, page_size: pageSize, status, filename },
    })
  },
}
