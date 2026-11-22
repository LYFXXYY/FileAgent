/**
 * 工作台状态管理 —— 持久化上传/处理/下载的中间状态
 *
 * 【设计思路】
 * - 用户在「工作台」上传文件并点击「开始处理」后，如果切换到其他页面再返回，
 *   前端状态会丢失（虽然后端处理仍在继续）。
 * - 本 store 将关键状态持久化到 sessionStorage，保证：
 *   1. 切换页面再返回时状态不丢失
 *   2. 刷新浏览器后状态可恢复
 *   3. 仅恢复 UI 状态，不重复触发后端请求
 *
 * 【持久化策略】
 * - sessionStorage：浏览器关闭后清除，适合临时会话状态
 * - 不持久化 File 对象（无法序列化），只持久化 log_id、uploadResult 等关键数据
 * - 使用标准 ref + watch 模式：任一状态变化自动写入 sessionStorage
 *   （不要用对象字面量 getter/setter 代理闭包变量——Pinia 会把访问器
 *    转换成普通数据属性，导致 restore 后 UI 拿不到恢复值、setter 不触发保存）
 */

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { GradingIssue, SpecRef, UploadResponse } from '@/api/types'

interface HomeState {
  currentStep: number
  selectedTaskId: number | null
  logId: number | null
  uploadResult: UploadResponse | null
  processing: boolean
  downloading: boolean
  processingError: string | null
  /**
   * 本轮等待的截止时间戳（ms）。null = 还没开始等或已清账。
   * 放在 store 而不是组件里，是为了让"切页再切回""刷新浏览器"都不能把它清零——
   * 否则超时上限形同虚设，用户可以靠来回切换无限等下去。
   */
  pollDeadlineAt: number | null
  processedContent: string | null
  gradingIssues: GradingIssue[] | null
  gradingDegraded: boolean
  specRefs: SpecRef[] | null
  specNote: string | null
}

const STORAGE_KEY = 'fileagent_home_state'

export const useHomeStore = defineStore('home', () => {
  // ---------- State（标准 ref，Pinia 返回后自动解包） ----------
  const currentStep = ref(0)
  const selectedTaskId = ref<number | null>(null)
  const logId = ref<number | null>(null)
  const uploadResult = ref<UploadResponse | null>(null)
  const processing = ref(false)
  const downloading = ref(false)
  const processingError = ref<string | null>(null)
  const pollDeadlineAt = ref<number | null>(null)
  const processedContent = ref<string | null>(null)
  const gradingIssues = ref<GradingIssue[] | null>(null)
  const gradingDegraded = ref(false)
  const specRefs = ref<SpecRef[] | null>(null)
  const specNote = ref<string | null>(null)

  // ---------- 持久化方法 ----------
  function save() {
    try {
      const state: HomeState = {
        currentStep: currentStep.value,
        selectedTaskId: selectedTaskId.value,
        logId: logId.value,
        uploadResult: uploadResult.value,
        processing: processing.value,
        downloading: downloading.value,
        processingError: processingError.value,
        pollDeadlineAt: pollDeadlineAt.value,
        processedContent: processedContent.value,
        gradingIssues: gradingIssues.value,
        gradingDegraded: gradingDegraded.value,
    specRefs: specRefs.value,
    specNote: specNote.value,
      }
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state))
    } catch (err) {
      // sessionStorage 可能配额不足或禁用，失败不影响主流程
      console.warn('sessionStorage save failed:', err)
    }
  }

  // 任一状态变化自动持久化（uploadResult 含嵌套对象，需要 deep）
  watch(
    [
      currentStep,
      selectedTaskId,
      logId,
      uploadResult,
      processing,
      downloading,
      processingError,
      pollDeadlineAt,
      processedContent,
      gradingIssues,
      gradingDegraded,
      specRefs,
      specNote,
    ],
    save,
    { deep: true }
  )

  function restore() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw) as Partial<HomeState>
        // 逐个字段恢复，缺省值回退到初始状态，保证类型安全
        currentStep.value = parsed.currentStep ?? 0
        selectedTaskId.value = parsed.selectedTaskId ?? null
        logId.value = parsed.logId ?? null
        uploadResult.value = parsed.uploadResult ?? null
        processing.value = parsed.processing ?? false
        downloading.value = parsed.downloading ?? false
        processingError.value = parsed.processingError ?? null
        pollDeadlineAt.value = parsed.pollDeadlineAt ?? null
        processedContent.value = parsed.processedContent ?? null
        gradingIssues.value = parsed.gradingIssues ?? null
        gradingDegraded.value = parsed.gradingDegraded ?? false
        specRefs.value = parsed.specRefs ?? null
        specNote.value = parsed.specNote ?? null
      }
    } catch (err) {
      console.warn('sessionStorage restore failed:', err)
      // 恢复失败时重置为初始状态
      clear()
    }
  }

  function clear() {
    sessionStorage.removeItem(STORAGE_KEY)
    currentStep.value = 0
    selectedTaskId.value = null
    logId.value = null
    uploadResult.value = null
    processing.value = false
    downloading.value = false
    processingError.value = null
    pollDeadlineAt.value = null
    processedContent.value = null
    gradingIssues.value = null
    gradingDegraded.value = false
    specRefs.value = null
    specNote.value = null
  }

  return {
    currentStep,
    selectedTaskId,
    logId,
    uploadResult,
    processing,
    downloading,
    processingError,
    pollDeadlineAt,
    processedContent,
    gradingIssues,
    gradingDegraded,
    specRefs,
    specNote,
    save,
    restore,
    clear,
  }
})
