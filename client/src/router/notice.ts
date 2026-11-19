/**
 * 守卫提示的投递
 *
 * 为什么要有这个模块：守卫里直接弹消息不可靠——被拦回首页时目标路由常常就是当前路由，
 * 靠 watch 路由变化根本不会触发（实测踩过）。统一改在 `router.afterEach` 里投递：
 * 每次导航完成都会跑一次，有账就弹、没账就什么都不做。
 *
 * 单独成文件（而不是放在 router/index.ts 里导出）是为了避开循环引用：
 * router 要 import AppLayout，AppLayout 再反过来 import router 就成环了。
 */

import { ElMessage } from 'element-plus'

let pending = ''
// app 挂载前弹的消息不会显示（实测冷加载就是这样：afterEach 跑在 mount 之前，
// 消息被消费掉却看不见）。所以投递要等 ready
let ready = false

export function setNotice(msg: string): void {
  pending = msg
}

/** 导航完成后调用：没就绪就先把话攒着，就绪后由 markReady 补投 */
export function flushNotice(): void {
  if (!pending || !ready) return
  ElMessage.warning(pending)
  pending = ''
}

/** AppLayout 挂载后调用一次：标记就绪，并把攒着的提示补投出去 */
export function markReady(): void {
  ready = true
  flushNotice()
}
