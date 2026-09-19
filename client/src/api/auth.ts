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
}
