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

  it('Pydantic 校验错误：字段名翻成中文界面叫法，msg 能译则译', () => {
    const err = axiosErrorWith(422, {
      detail: [
        {
          msg: 'String should have at least 8 characters',
          loc: ['body', 'password'],
          type: 'string_too_short',
        },
      ],
    })
    expect(extractErrorMessage(err)).toBe('密码：长度不足，至少需要 8 个字符')
  })

  it('校验错误的字段不在中文表里时，只丢英文 msg 而不带内部字段名', () => {
    const err = axiosErrorWith(422, { detail: [{ msg: '字段长度不够', loc: ['body', 'zzz'] }] })
    expect(extractErrorMessage(err)).toBe('字段长度不够')
  })

  it('detail 缺失时按状态码兜底，不把英文原文（如 Internal Server Error）丢给用户', () => {
    const err = axiosErrorWith(500, { error: 'boom' }, 'Internal Server Error')
    expect(extractErrorMessage(err)).toBe('服务器处理时出错，详细信息已记录，请稍后重试')

    const tooLarge = axiosErrorWith(413, undefined as unknown, 'Request Entity Too Large')
    expect(extractErrorMessage(tooLarge)).toContain('文件太大')
  })

  it('响应体是 Blob（下载接口失败）时也能给出人话', () => {
    const err = axiosErrorWith(404, new Blob(['x']), 'Request failed with status code 404')
    expect(extractErrorMessage(err)).toBe('要操作的对象不存在，可能已被删除')
  })

  it('429 带上额度语义，用户才知道不是系统坏了', () => {
    const err = axiosErrorWith(429, { detail: '最近 24 小时已处理 20 次（E-QUOTA-PROCESS）' })
    expect(extractErrorMessage(err)).toContain('E-QUOTA-PROCESS')
  })

  it('有响应且 detail 是空串时也走状态码兜底', () => {
    const err = axiosErrorWith(400, { detail: '' }, 'Bad Request')
    expect(extractErrorMessage(err)).toBe('请求没有被接受，请检查填写内容后重试')
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
