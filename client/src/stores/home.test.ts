/**
 * 工作台 store 测试：持久化往返 + gradingIssues 的 null / [] 语义分界
 *
 * 后者是真出过的缺陷：非批改任务的清单是 null，一旦和"批改了但 0 条"的 []
 * 混为一谈，界面就会对没批改的文档说"批改完成，未发现问题"。
 */
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it } from 'vitest'

import type { GradingIssue } from '@/api/types'
import { useHomeStore } from './home'

const KEY = 'fileagent_home_state'

const sampleIssue: GradingIssue = {
  type: 'typo',
  original: '高兴的跳了起来',
  suggestion: '高兴地跳了起来',
  reason: '的/地误用',
  confidence: 0.9,
  start: 3,
  end: 10,
}

beforeEach(() => {
  setActivePinia(createPinia())
  sessionStorage.clear()
})

describe('useHomeStore 持久化', () => {
  it('save 后改脏，restore 能原样拿回', () => {
    const store = useHomeStore()
    store.currentStep = 2
    store.selectedTaskId = 7
    store.logId = 42
    store.processedContent = '修订后的全文'
    store.save()

    store.currentStep = 0
    store.selectedTaskId = null
    store.logId = null
    store.processedContent = null
    store.restore()

    expect(store.currentStep).toBe(2)
    expect(store.selectedTaskId).toBe(7)
    expect(store.logId).toBe(42)
    expect(store.processedContent).toBe('修订后的全文')
  })

  it('状态变化自动落盘（ref + watch 模式，不用手调 save）', async () => {
    const store = useHomeStore()
    store.currentStep = 3
    await nextTick()

    const raw = sessionStorage.getItem(KEY)
    expect(raw).toBeTruthy()
    expect(JSON.parse(raw as string).currentStep).toBe(3)
  })

  it('嵌套的 uploadResult 变化也要落盘（watch 必须 deep）', async () => {
    const store = useHomeStore()
    store.uploadResult = {
      log_id: 11,
      original_filename: 'a.docx',
      content_length: 100,
      extracted_text: '正文',
      status: 'pending',
      uploaded_at: '2026-11-18T10:00:00',
    }
    await nextTick()

    expect(JSON.parse(sessionStorage.getItem(KEY) as string).uploadResult.log_id).toBe(11)
  })

  it('clear 同时清掉 sessionStorage 与内存状态', () => {
    const store = useHomeStore()
    store.logId = 5
    store.save()
    store.clear()

    expect(sessionStorage.getItem(KEY)).toBeNull()
    expect(store.logId).toBeNull()
    expect(store.currentStep).toBe(0)
    expect(store.gradingIssues).toBeNull()
  })

  it('存了坏 JSON 时不抛，退回初始状态', () => {
    sessionStorage.setItem(KEY, '{这不是合法 json')
    const store = useHomeStore()
    store.currentStep = 1

    expect(() => store.restore()).not.toThrow()
    expect(store.currentStep).toBe(0)
  })

  it('没有存档时 restore 不动现有状态', () => {
    const store = useHomeStore()
    store.logId = 9
    store.restore()
    expect(store.logId).toBe(9)
  })
})

describe('gradingIssues 的 null 与 [] 必须各自保真', () => {
  it('null（非批改任务）存取自洽，不会被当成空清单', () => {
    const store = useHomeStore()
    store.gradingIssues = null
    store.save()

    store.gradingIssues = [sampleIssue]
    store.restore()

    expect(store.gradingIssues).toBeNull()
  })

  it('[]（批改了但没发现问题）恢复后仍是空数组，不是 null', () => {
    const store = useHomeStore()
    store.gradingIssues = []
    store.save()

    store.gradingIssues = [sampleIssue]
    store.restore()

    expect(store.gradingIssues).toEqual([])
    expect(store.gradingIssues).not.toBeNull()
  })

  it('有清单时逐字段还原，定位偏移不丢', () => {
    const store = useHomeStore()
    store.gradingIssues = [sampleIssue]
    store.save()

    // 用 $patch 而不是直接赋值：后者会让 TS 把类型收窄成 null，读回来时反而测不到东西
    store.$patch({ gradingIssues: null })
    store.restore()

    expect(store.gradingIssues).toHaveLength(1)
    expect(store.gradingIssues?.[0].start).toBe(3)
    expect(store.gradingIssues?.[0].end).toBe(10)
  })
})
