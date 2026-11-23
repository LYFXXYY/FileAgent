/**
 * 口令策略：说明文案与校验函数只放一份
 *
 * 权威判据在后端（`schemas/auth.py:_reject_weak_password_value`），这里是它的界面镜像。
 * 登录页的注册 tab 与改密弹窗都从这里取——两处各写一份迟早会漂，漂了就是
 * "注册挡得住的弱口令，改密口设得进去"。
 */

/** 与后端 `RegisterRequest.password` 的 Field 约束逐字对应 */
export const PASSWORD_MIN = 8
export const PASSWORD_MAX = 72

/** 给用户看的完整规则（常驻说明用，不要塞进 placeholder：超出框宽会被省略号截断） */
export const weakPasswordHint = `密码至少 ${PASSWORD_MIN} 位，且不能只用数字或只用字母`

/** 输入框占位符：短到在任何卡片宽度下都显示得完整 */
export const passwordPlaceholder = `至少 ${PASSWORD_MIN} 位，混入数字或符号`

type Validator = (rule: unknown, value: string, callback: (e?: Error) => void) => void

/**
 * 只挡"整串只有一个字符类"：纯数字（生日、手机号）与纯字母。
 * 不用复杂度那套（大小写+数字+符号），它逼出 `Password123!`，既难记又是最常见的弱口令模板。
 */
export const checkPasswordStrength: Validator = (_rule, value, callback) => {
  if (!value) return callback()
  if (/^\d+$/.test(value)) return callback(new Error('密码不能只用数字'))
  if (/^[\p{L}]+$/u.test(value)) return callback(new Error('密码不能只用字母，请混入数字或符号'))
  return callback()
}

/** 二次输入一致性校验：新密码与确认必须同串 */
export function makeConfirmValidator(getPassword: () => string): Validator {
  return (_rule, value, callback) => {
    if (!value) return callback()
    if (value !== getPassword()) return callback(new Error('两次输入的新密码不一致'))
    return callback()
  }
}
