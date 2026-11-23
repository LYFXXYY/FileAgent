/**
 * 口令策略纯逻辑测试
 *
 * 这个模块是登录页与改密弹窗共用的判据，出问题的方式是"前端放过、后端 422"或反过来。
 * 所以钉两件事：字符类判据的形状，以及它给出的文案与后端一致。
 */
import { describe, expect, it } from 'vitest'

import {
  PASSWORD_MAX,
  PASSWORD_MIN,
  checkPasswordStrength,
  makeConfirmValidator,
  weakPasswordHint,
} from './passwordPolicy'

/** Element 的 validator 用回调报错，这里包一层好断言 */
function run(value: string): string | null {
  let error: string | null = null
  checkPasswordStrength({}, value, (e?: Error) => {
    if (e) error = e.message
  })
  return error
}

describe('checkPasswordStrength', () => {
  it('空值放过：必填由 required 规则管，强度不重复报错', () => {
    expect(run('')).toBeNull()
  })

  it('纯数字与纯字母各拦一次，文案与后端同义', () => {
    expect(run('12345678')).toBe('密码不能只用数字')
    expect(run('abcdefgh')).toBe('密码不能只用字母，请混入数字或符号')
  })

  it('混入两个字符类就放过（长度由 min/max 规则管）', () => {
    expect(run('pass1234')).toBeNull()
    expect(run('密码好123')).toBeNull()
  })

  it('中文整串也算"只用字母"：\\p{L} 含汉字，不能因为不是 ASCII 就漏过去', () => {
    expect(run('只用汉字行不行')).toBe('密码不能只用字母，请混入数字或符号')
  })
})

describe('文案与阈值', () => {
  it('提示里写出的数字就是实际阈值', () => {
    expect(weakPasswordHint).toContain(String(PASSWORD_MIN))
    expect(PASSWORD_MAX).toBe(72) // bcrypt 的上限，改了要和后端 Field 一起改
  })
})

describe('makeConfirmValidator', () => {
  it('两次不一致才报错，空确认由必填规则管', () => {
    const validator = makeConfirmValidator(() => 'abc12345')
    let error: string | null = null
    validator({}, 'abc12345', (e?: Error) => {
      if (e) error = e.message
    })
    expect(error).toBeNull()

    validator({}, 'abc1234', (e?: Error) => {
      if (e) error = e.message
    })
    expect(error).toBe('两次输入的新密码不一致')

    error = '未重置'
    validator({}, '', (e?: Error) => {
      if (e) error = e.message
    })
    expect(error).toBe('未重置')
  })
})
