/**
 * 管理端 API —— 仅 admin 登录态可用（后端 require_admin 兜底，403 会走拦截器提示）
 */

import { http } from './client'
import type { AdminUserRow, UserRole } from './types'

export const adminApi = {
  listUsers: () => http.get<AdminUserRow[]>('/admin/users'),

  updateRole: (userId: number, role: UserRole) =>
    http.patch<AdminUserRow>(`/admin/users/${userId}/role`, { role }),

  updateDefaultAccess: (userId: number, can_use_default: boolean) =>
    http.patch<AdminUserRow>(`/admin/users/${userId}/default-access`, { can_use_default }),
}
