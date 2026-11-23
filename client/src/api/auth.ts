/**
 * 认证相关的 API 调用
 *
 * 【Java 对比】
 * - 类似 Feign 声明式 HTTP Client：@FeignClient + @PostMapping
 */

import { http } from './client'
import type { TokenResponse, User } from './types'

export const authApi = {
  register(username: string, password: string) {
    return http.post<User>('/auth/register', { username, password })
  },
  login(username: string, password: string) {
    return http.post<TokenResponse>('/auth/login', { username, password })
  },
  /** 启动时刷新 role/can_use_default，管理员的变更无需用户重登 */
  me() {
    return http.get<User>('/auth/me')
  },
  /** 自助改密：改的永远是 token 里那个人，请求体不带 user_id */
  changePassword(oldPassword: string, newPassword: string) {
    return http.post<void>('/auth/change-password', {
      old_password: oldPassword,
      new_password: newPassword,
    })
  },
}
