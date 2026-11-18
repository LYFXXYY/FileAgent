/**
 * API 层纯逻辑测试：错误消息提取 + token 存储
 *
 * 这两处是全站所有错误提示和登录态的唯一出口，出过问题的方式是
 * "后端换了错误结构，前端弹 undefined"，所以值得钉住。
 */
import { AxiosError } from 'axios'
import { beforeEach, describe, expect, it } from 'vitest'

import { extractErrorMessage, tokenStorage } from './client'

function axiosErrorWith(status: number, data: unknown, message = 'Request failed'): AxiosError {
  return new AxiosError(message, 'ERR_BAD_REQUEST', undefined, undefined, {
    status,
    statusText: '',
    data,
    headers: {},
    config: { headers: {} } as never,
  })
}

describe('extractErrorMessage', () => {
  it('透出 FastAPI 的字符串 detail 原文', () => {
    const err = axiosErrorWith(400, { detail: '只允许 .docx / .pdf' })
    expect(extractErrorMessage(err)).toBe('只允许 .docx / .pdf')
  })

  it('Pydantic 校验错误的 detail 是数组时，取第一条 msg', () => {
    const err = axiosErrorWith(422, { detail: [{ msg: '字段长度不够', loc: ['body', 'name'] }] })
    expect(extractErrorMessage(err)).toBe('字段长度不够')
  })

  it('有响应但没有 detail 时退回 axios 自身消息', () => {
    const err = axiosErrorWith(500, { error: 'boom' }, 'Internal Server Error')
    expect(extractErrorMessage(err)).toBe('Internal Server Error')
  })

  it('无响应（断网/超时）时不能返回空串', () => {
    const err = new AxiosError('Network Error', 'ERR_NETWORK')
    expect(extractErrorMessage(err)).toBe('Network Error')
  })

  it('非 axios 错误也能处理', () => {
    expect(extractErrorMessage(new Error('普通异常'))).toBe('普通异常')
    expect(extractErrorMessage('字符串抛错')).toBe('字符串抛错')
    expect(extractErrorMessage(undefined)).toBe('undefined')
  })
})

describe('tokenStorage', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('写入后能读回同一个值', () => {
    tokenStorage.set('abc.def.ghi')
    expect(tokenStorage.get()).toBe('abc.def.ghi')
  })

  it('clear 之后读到 null（拦截器据此不再带 Authorization）', () => {
    tokenStorage.set('abc')
    tokenStorage.clear()
    expect(tokenStorage.get()).toBeNull()
  })

  it('未写过时读到 null，不抛', () => {
    expect(tokenStorage.get()).toBeNull()
  })
})
