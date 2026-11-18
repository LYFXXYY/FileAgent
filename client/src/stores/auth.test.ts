/**
 * 认证 store 测试：token 双写约定 + 身份快照免重登
 *
 * 两条铁律都在这里：
 * 1) 登录态要同时写 store 和 localStorage，因为 axios 拦截器与路由守卫拿不到 store；
 * 2) /auth/me 刷新是"可选增强"，失败必须静默，不能把启动拖死。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authApi } from '@/api/auth'
import { useAuthStore } from './auth'

vi.mock('@/api/auth', () => ({
  authApi: {
    login: vi.fn(),
    register: vi.fn(),
    me: vi.fn(),
  },
}))

const mockedLogin = vi.mocked(authApi.login)
const mockedMe = vi.mocked(authApi.me)

function tokenPayload(over: Partial<Record<string, unknown>> = {}) {
  return {
    data: {
      access_token: 'jwt.token.here',
      username: 'LYF',
      role: 'admin',
      can_use_default: true,
      token_type: 'bearer',
      ...over,
    },
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
})

describe('useAuthStore 登录态', () => {
  it('登录成功同时写 store 与 localStorage（拦截器只读后者）', async () => {
    mockedLogin.mockResolvedValue(tokenPayload() as never)
    const store = useAuthStore()

    await store.login('LYF', '123456')

    expect(store.token).toBe('jwt.token.here')
    expect(store.isLoggedIn).toBe(true)
    expect(localStorage.getItem('fileagent_token')).toBe('jwt.token.here')
    expect(localStorage.getItem('fileagent_username')).toBe('LYF')
    expect(localStorage.getItem('fileagent_role')).toBe('admin')
    expect(localStorage.getItem('fileagent_can_use_default')).toBe('true')
  })

  it('isAdmin 由 role 派生，普通用户为 false', async () => {
    mockedLogin.mockResolvedValue(
      tokenPayload({ role: 'user', can_use_default: false, username: 'plain_test' }) as never,
    )
    const store = useAuthStore()

    await store.login('plain_test', '123456')

    expect(store.isAdmin).toBe(false)
    expect(store.canUseDefault).toBe(false)
  })

  it('退出把四处登录态全部清掉', async () => {
    mockedLogin.mockResolvedValue(tokenPayload() as never)
    const store = useAuthStore()
    await store.login('LYF', '123456')

    store.logout()

    expect(store.token).toBeNull()
    expect(store.username).toBeNull()
    expect(store.isAdmin).toBe(false)
    expect(localStorage.getItem('fileagent_token')).toBeNull()
    expect(localStorage.getItem('fileagent_username')).toBeNull()
    expect(localStorage.getItem('fileagent_role')).toBeNull()
    expect(localStorage.getItem('fileagent_can_use_default')).toBeNull()
  })

  it('登录失败抛出的是后端 detail，不是 axios 原文', async () => {
    const { AxiosError } = await import('axios')
    mockedLogin.mockRejectedValue(
      new AxiosError(
        'Request failed',
        'ERR_BAD_REQUEST',
        undefined,
        undefined,
        { status: 401, statusText: '', data: { detail: '用户名或密码错误' }, headers: {}, config: { headers: {} } as never },
      ) as never,
    )
    const store = useAuthStore()

    await expect(store.login('LYF', 'bad')).rejects.toThrow('用户名或密码错误')
    expect(store.isLoggedIn).toBe(false)
  })
})

describe('refreshProfile（管理员改授权后无需重登）', () => {
  it('用后端最新值覆盖本地快照', async () => {
    mockedLogin.mockResolvedValue(
      tokenPayload({ role: 'user', can_use_default: false }) as never,
    )
    const store = useAuthStore()
    await store.login('someone', '123456')
    expect(store.isAdmin).toBe(false)

    mockedMe.mockResolvedValue(
      tokenPayload({ role: 'admin', can_use_default: true, username: 'someone' }) as never,
    )
    await store.refreshProfile()

    expect(store.isAdmin).toBe(true)
    expect(store.canUseDefault).toBe(true)
    expect(localStorage.getItem('fileagent_role')).toBe('admin')
  })

  it('刷新失败必须静默，保留旧快照继续用', async () => {
    mockedLogin.mockResolvedValue(tokenPayload() as never)
    const store = useAuthStore()
    await store.login('LYF', '123456')

    mockedMe.mockRejectedValue(new Error('网络断了') as never)
    await expect(store.refreshProfile()).resolves.toBeUndefined()

    expect(store.isAdmin).toBe(true)
    expect(localStorage.getItem('fileagent_role')).toBe('admin')
  })

  it('未登录时不发请求', async () => {
    const store = useAuthStore()
    await store.refreshProfile()
    expect(mockedMe).not.toHaveBeenCalled()
  })
})
