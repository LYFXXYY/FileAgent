<script setup lang="ts">
/**
 * 工作台 —— 上传 → 处理 → 下载 三步流程
 *
 * 【设计思路】
 * - 页面用 el-steps 展示进度，一次只显示当前步骤的操作控件
 * - 视觉优化：卡片式布局 + SVG 图标 + 悬停动效 + 渐变背景
 * - 简约科技风：浅色背景、清晰边框、适度阴影、统一圆角
 *
 * 【轮询机制】
 * - 处理中如果浏览器刷新或 axios 超时，后端可能仍在处理
 * - 通过 setInterval 定期查询 status 接口，完成后自动推进到下载步骤
 * - 组件失活时暂停轮询，重新激活时恢复
 */
import { computed, onActivated, onDeactivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { processApi } from '@/api/process'
import { providerApi } from '@/api/providers'
import { taskApi } from '@/api/tasks'
import type { GradingIssue, Provider, Task } from '@/api/types'
import { useHomeStore } from '@/stores/home'

const homeStore = useHomeStore()

// 步骤：0 选任务 → 1 上传 → 2 处理 → 3 完成可下载
const currentStep = ref(homeStore.currentStep)

// 各步的状态（从 store 双向同步，保证切换页面或刷新后状态不丢失）
const tasks = ref<Task[]>([])
const providers = ref<Provider[]>([])
const selectedTaskId = ref(homeStore.selectedTaskId)
const pickedFile = ref<File | null>(null)
const uploadResult = ref(homeStore.uploadResult)
const uploading = ref(false) // 上传中的 loading 状态（大文件可能需要几秒）
const processing = ref(homeStore.processing)
const downloading = ref(homeStore.downloading)
const processingError = ref(homeStore.processingError)
const processedContent = ref(homeStore.processedContent)
const gradingIssues = ref(homeStore.gradingIssues)
const gradingDegraded = ref(homeStore.gradingDegraded)

// ---------- 结构化批改渲染 ----------
// 按问题类型上色，前端只认 8 个 canonical 值（与 schemas/grading.py 的 ISSUE_TYPES 对齐）
const TYPE_COLOR: Record<string, string> = {
  typo: '#dc2626', // 红
  grammar: '#ea580c', // 橙
  wording: '#d97706', // 黄
  punctuation: '#65a30d', // 黄绿
  structure: '#0891b2', // 蓝绿
  logic: '#4f46e5', // 靛
  content: '#7c3aed', // 紫
  other: '#64748b', // 灰
}

// 问题类型的显示文案（与 schemas/grading.py 的 ISSUE_TYPES 一一对应）
const TYPE_LABEL: Record<string, string> = {
  typo: '错别字',
  grammar: '语法',
  wording: '用词',
  punctuation: '标点',
  structure: '结构',
  logic: '逻辑',
  content: '内容',
  other: '其它',
}

// 点卡片 → 高亮原文中的对应片段，点片段 → 卡片上高亮
const activeIssueIndex = ref<number | null>(null)

interface Span {
  text: string;
  issue?: GradingIssue;
  index: number | null;
}
interface Para { segs: Span[] }

// 把原文按段落切开，按 start/end 匹配把问题片段套上 span
// 找不到位置的（后端匹配失败）保持未定位，绝不猜
function buildParagraphs(
  text: string,
  issues: GradingIssue[] | null,
): Para[] {
  if (!text) return []
  if (!issues)
    return text
      .split('\n')
      .map((line) => ({ segs: [{ text: line, index: null }] }))

  const lines = text.split('\n')
  const offsets: number[] = []
  lines.reduce((acc, l) => {
    offsets.push(acc)
    return acc + l.length + 1
  }, 0)

  const out: Para[] = []
  for (const [i, line] of lines.entries()) {
    const start = offsets[i]
    const end = start + line.length
    const marks = issues
      .map((issue, idx) => ({ issue, idx }))
      .filter(
        ({ issue }) =>
          issue.start !== null &&
          issue.end !== null &&
          issue.start! < end &&
          issue.end! > start,
      )
      .sort((a, b) => a.issue.start! - b.issue.start!)
      .map(({ issue, idx }) => ({
        s: Math.max(0, issue.start! - start),
        e: Math.min(line.length, issue.end! - start),
        issue,
        idx,
      }))

    const segs: Span[] = []
    let last = 0
    for (const m of marks) {
      if (m.s > last) segs.push({ text: line.slice(last, m.s), index: null })
      segs.push({ text: line.slice(m.s, m.e), issue: m.issue, index: m.idx })
      last = m.e
    }
    if (last < line.length) segs.push({ text: line.slice(last), index: null })
    out.push({ segs })
  }
  return out
}

const displayTextParas = computed(() =>
  buildParagraphs(
    uploadResult.value?.extracted_text || '',
    gradingIssues.value,
  ),
)

// 点击卡片，滚动到原文里的对应位置
function onIssueClick(idx: number) {
  activeIssueIndex.value = idx
  document
    .getElementById(`issue-anchor-${idx}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// 可用模型提供商数量（为 0 时在选任务步骤提示用户先配置）
const activeProviderCount = computed(() =>
  providers.value.filter((p) => p.is_active).length
)

// ---------- 轮询机制 ----------
// 处理中如果浏览器刷新或 axios 超时，后端可能仍在处理
// 通过定期查询 status 接口，完成后自动推进到下载步骤
const POLL_INTERVAL = 5000 // 5 秒轮询一次
const MAX_POLL_COUNT = 120 // 最多轮询 120 次（10 分钟）
let pollingTimer: ReturnType<typeof setInterval> | null = null
let pollCount = 0

function startPolling() {
  stopPolling()
  pollCount = 0
  pollingTimer = setInterval(pollOnce, POLL_INTERVAL)
}

function stopPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer)
    pollingTimer = null
  }
}

async function pollOnce() {
  pollCount++
  if (pollCount > MAX_POLL_COUNT) {
    stopPolling()
    processing.value = false
    processingError.value = '等待超时，请稍后在操作日志中查看结果'
    syncToStore()
    return
  }

  const logId = homeStore.logId
  if (!logId) return

  try {
    const resp = await processApi.status(logId)
    if (resp.data.status === 'done') {
      stopPolling()
      currentStep.value = 3
      processing.value = false
      applyResult(resp.data)
      ElMessage.success('处理完成')
      syncToStore()
    } else if (resp.data.status === 'failed') {
      stopPolling()
      processing.value = false
      processingError.value = resp.data.error_message ?? '处理失败'
      ElMessage.error(processingError.value)
      syncToStore()
    }
    // status === 'processing' → 继续轮询
  } catch {
    // 查询失败，继续轮询（可能网络抖动）
  }
}

// 将本地 ref 同步回 store，store 会负责持久化到 sessionStorage
function syncToStore() {
  homeStore.currentStep = currentStep.value
  homeStore.selectedTaskId = selectedTaskId.value
  homeStore.uploadResult = uploadResult.value
  homeStore.processing = processing.value
  homeStore.downloading = downloading.value
  homeStore.processingError = processingError.value
  homeStore.processedContent = processedContent.value
  homeStore.gradingIssues = gradingIssues.value
  homeStore.gradingDegraded = gradingDegraded.value
}

// 把一次处理成功的响应内容铺到 UI：正文 + 问题清单 + 降级标记
function applyResult(resp: { processed_content: string | null; issues: GradingIssue[] | null; grading_degraded: boolean }) {
  processedContent.value = resp.processed_content
  gradingIssues.value = resp.issues
  gradingDegraded.value = resp.grading_degraded
}

// ---------- 生命周期 ----------
onMounted(async () => {
  // 先从 sessionStorage 恢复 store 中的持久化状态
  homeStore.restore()

  // 把 store 中的状态同步到本地 UI
  currentStep.value = homeStore.currentStep
  selectedTaskId.value = homeStore.selectedTaskId
  uploadResult.value = homeStore.uploadResult
  processing.value = homeStore.processing
  downloading.value = homeStore.downloading
  processingError.value = homeStore.processingError
  processedContent.value = homeStore.processedContent
  gradingIssues.value = homeStore.gradingIssues
  gradingDegraded.value = homeStore.gradingDegraded
  pickedFile.value = null // File 对象不可序列化，恢复时重置

  // 如果恢复时仍处于处理中，查询一次后端真实状态，避免前端显示 stale 状态
  await refreshProcessingStatus()

  try {
    const [taskResp, providerResp] = await Promise.all([
      taskApi.list(),
      providerApi.list(),
    ])
    tasks.value = taskResp.data
    providers.value = providerResp.data
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
})

// keep-alive 下切换回工作台时，刷新处理状态和任务列表
onActivated(async () => {
  // 刷新后端处理状态（若仍在处理中，可能已经完成）
  await refreshProcessingStatus()

  // 刷新任务列表和提供商列表（其他页面可能新增/修改了）
  try {
    const [taskResp, providerResp] = await Promise.all([
      taskApi.list(),
      providerApi.list(),
    ])
    tasks.value = taskResp.data
    providers.value = providerResp.data
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
})

// 组件失活时暂停轮询，避免不必要的 API 调用
onDeactivated(() => {
  stopPolling()
})

// 查询后端最新处理状态，自动推进到完成步骤
async function refreshProcessingStatus() {
  const logId = homeStore.logId
  if (currentStep.value === 2 && logId !== null && homeStore.processing) {
    try {
      const resp = await processApi.status(logId)
      if (resp.data.status === 'done') {
        stopPolling()
        currentStep.value = 3
        processing.value = false
        applyResult(resp.data)
        syncToStore()
      } else if (resp.data.status === 'failed') {
        stopPolling()
        processing.value = false
        processingError.value = resp.data.error_message ?? '处理失败'
        syncToStore()
      } else {
        // 仍在处理中，启动轮询定期检查
        startPolling()
      }
    } catch (err) {
      // 查询失败不影响主流程，保持当前状态
      ElMessage.error(extractErrorMessage(err))
    }
  }
}

// ---------- 步骤 1：选任务 ----------
function onTaskChange() {
  currentStep.value = 1
}

// ---------- 步骤 2：选文件 + 上传 ----------
function onFileChange(file: { raw?: File }) {
  // el-upload 的 on-change 会给到包装对象，raw 是原生 File
  if (file.raw) {
    pickedFile.value = file.raw
  }
  return false // 返回 false 阻止 el-upload 自动上传，改由我们手动控制
}

function onUploadExceed(files: FileList | File[]) {
  // el-upload 达到 limit 后再次选择文件时不会触发 on-change，
  // 而是触发 on-exceed；这里手动用新文件替换已选文件
  const file = Array.isArray(files) ? files[0] : files.item(0)
  if (file) {
    pickedFile.value = file
  }
}

async function doUpload() {
  if (!selectedTaskId.value || !pickedFile.value) return
  uploading.value = true
  try {
    const resp = await processApi.upload(selectedTaskId.value, pickedFile.value)
    uploadResult.value = resp.data
    homeStore.logId = resp.data.log_id // 保存 log_id 用于后续状态查询
    currentStep.value = 2
    syncToStore()
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    uploading.value = false
  }
}

// ---------- 步骤 3：处理 ----------
async function doProcess() {
  if (!uploadResult.value) return
  processing.value = true
  processingError.value = null
  syncToStore()
  try {
    const resp = await processApi.process(uploadResult.value.log_id)
    if (resp.data.status === 'done') {
      // 极少数情况下后端可能已同步完成
      stopPolling()
      currentStep.value = 3
      processing.value = false
      applyResult(resp.data)
      ElMessage.success('处理完成')
      syncToStore()
    } else if (resp.data.status === 'failed') {
      processing.value = false
      processingError.value = resp.data.error_message ?? '处理失败'
      ElMessage.error(processingError.value)
      syncToStore()
    } else {
      // processing：后端已转入后台处理，启动轮询等结果
      // （后台任务与请求解耦，即使刷新/断网也不影响处理继续）
      startPolling()
      ElMessage.info('正在处理，请稍候...')
      syncToStore()
    }
  } catch {
    // 网络错误：后端可能已在后台处理，保持 processing 并启动轮询
    processingError.value = null
    startPolling()
    ElMessage.info('正在等待处理结果，请稍候...')
    syncToStore()
  }
}

// ---------- 步骤 4：下载 ----------
async function doDownload() {
  if (!uploadResult.value) return
  downloading.value = true
  try {
    const resp = await processApi.download(uploadResult.value.log_id)
    // 浏览器 blob 下载：造 URL + 挂 <a> + 触发点击
    const url = URL.createObjectURL(resp.data)
    const a = document.createElement('a')
    a.href = url
    a.download = `result_${uploadResult.value.original_filename}`
    a.click()
    URL.revokeObjectURL(url) // 释放内存
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    downloading.value = false
  }
}

// ---------- 步骤导航 ----------
function goBackToUpload() {
  // 从处理步骤回到上传步骤（保留选中的任务）
  stopPolling()
  processing.value = false
  processingError.value = null
  currentStep.value = 1
  syncToStore()
}

function processNewFile() {
  // 从下载步骤回到上传步骤，保留选中的任务
  stopPolling()
  currentStep.value = 1
  pickedFile.value = null
  uploadResult.value = null
  processing.value = false
  processingError.value = null
  processedContent.value = null
  gradingIssues.value = null
  gradingDegraded.value = false
  homeStore.logId = null
  syncToStore()
}

// ---------- 重置 ----------
function reset() {
  stopPolling()
  currentStep.value = 0
  selectedTaskId.value = null
  pickedFile.value = null
  uploadResult.value = null
  uploading.value = false
  processing.value = false
  downloading.value = false
  processingError.value = null
  processedContent.value = null
  gradingIssues.value = null
  gradingDegraded.value = false
  homeStore.clear() // 清空 store 中的持久化状态
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">工作台</h2>
        <p class="page-subtitle">选择任务 → 上传文档 → LLM 处理 → 下载结果</p>
      </div>
      <el-button v-if="currentStep > 0" @click="reset">重新开始</el-button>
    </div>

    <!-- 步骤进度条：自定义样式，增加图标和过渡动效 -->
    <div class="steps-wrapper">
      <el-steps :active="currentStep" finish-status="success" align-center>
        <el-step title="选择任务" />
        <el-step title="上传文档" />
        <el-step title="AI 处理" />
        <el-step title="下载结果" />
      </el-steps>
    </div>

    <div class="step-body">
      <!-- Step 0: 选任务 -->
      <template v-if="currentStep === 0">
        <div class="step-card fade-in">
          <div class="card-icon">
            <!-- SVG: 任务列表图标 -->
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M8 6H21" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M8 12H21" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M8 18H21" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M3 6L3.01 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M3 12L3.01 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M3 18L3.01 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <h3>选择处理任务</h3>
          <p class="card-desc">从已创建的任务中选择一个，开始文档处理流程</p>
          <el-select
            v-model="selectedTaskId"
            placeholder="请选择任务"
            style="width: 100%"
            size="large"
            @change="onTaskChange"
          >
            <el-option
              v-for="t in tasks"
              :key="t.id"
              :label="t.name"
              :value="t.id"
            />
          </el-select>
          <p v-if="tasks.length === 0" class="empty-hint">
            还没有任务，先去 <router-link to="/tasks">任务管理</router-link> 创建一个
          </p>
          <el-alert
            v-if="tasks.length > 0 && activeProviderCount === 0"
            type="warning"
            :closable="false"
            show-icon
            style="margin-top: 12px"
          >
            未配置可用的模型提供商，处理会失败。请先去
            <router-link to="/providers">模型提供商</router-link> 添加
          </el-alert>
        </div>
      </template>

      <!-- Step 1: 上传文件 -->
      <template v-else-if="currentStep === 1">
        <div class="step-card fade-in">
          <div class="card-icon upload-icon">
            <!-- SVG: 上传图标 -->
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M21 15V19C21 19.5304 20.7893 20.0391 20.4142 20.4142C20.0391 20.7893 19.5304 21 19 21H5C4.46957 21 3.96086 20.7893 3.58579 20.4142C3.21071 20.0391 3 19.5304 3 19V15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M17 8L12 3L7 8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M12 3V15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <h3>上传文档</h3>
          <p class="card-desc">当前支持 .docx / .pdf 文字型文档，拖拽或点击上传</p>
          <el-upload
            drag
            accept=".docx,.pdf"
            :auto-upload="false"
            :show-file-list="false"
            :limit="1"
            :on-exceed="onUploadExceed"
            :on-change="onFileChange"
            class="upload-area"
          >
            <el-icon class="el-icon--upload"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M14 2H6C5.46957 2 4.96086 2.21071 4.58579 2.58579C4.21071 2.96086 4 3.46957 4 4V20C4 20.5304 4.21071 21.0391 4.58579 21.4142C4.96086 21.7893 5.46957 22 6 22H18C18.5304 22 19.0391 21.7893 19.4142 21.4142C19.7893 21.0391 20 20.5304 20 20V8L14 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M14 2V8H20" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M12 18V12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M9 15L12 12L15 15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg></el-icon>
            <div class="el-upload__text">
              拖拽 .docx / .pdf 文件到此处，或 <em>点击选择</em>
            </div>
          </el-upload>
          <div v-if="pickedFile" class="picked-file">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 4px;">
              <path d="M14 2H6C5.46957 2 4.96086 2.21071 4.58579 2.58579C4.21071 2.96086 4 3.46957 4 4V20C4 20.5304 4.21071 21.0391 4.58579 21.4142C4.96086 21.7893 5.46957 22 6 22H18C18.5304 22 19.0391 21.7893 19.4142 21.4142C19.7893 21.0391 20 20.5304 20 20V8L14 2Z" stroke="#409eff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M14 2V8H20" stroke="#409eff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            已选择：<code>{{ pickedFile.name }}</code>
            <span class="file-size">（{{ (pickedFile.size / 1024).toFixed(1) }} KB）</span>
          </div>
          <div class="step-actions">
            <el-button @click="currentStep = 0">上一步</el-button>
            <el-button
              type="primary"
              :disabled="!pickedFile"
              :loading="uploading"
              @click="doUpload"
            >
              {{ uploading ? '上传中...' : '上传' }}
            </el-button>
          </div>
        </div>
      </template>

      <!-- Step 2: 处理 -->
      <template v-else-if="currentStep === 2">
        <div class="step-card fade-in">
          <div class="card-icon process-icon">
            <!-- SVG: AI 处理图标 -->
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M12 2L2 7L12 12L22 7L12 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M2 17L12 22L22 17" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M2 12L12 17L22 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <h3>调用 LLM 处理</h3>
          <p class="card-desc">
            文件 <code>{{ uploadResult?.original_filename }}</code> 已上传，
            提取到 {{ uploadResult?.content_length }} 字符
          </p>
          <!-- 提取文本预览 -->
          <el-collapse v-if="uploadResult?.extracted_text" style="margin-bottom: 16px; text-align: left;">
            <el-collapse-item title="预览提取的文本" name="preview">
              <div class="text-preview">{{ uploadResult.extracted_text }}</div>
            </el-collapse-item>
          </el-collapse>
          <p class="hint warning">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 4px;">
              <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>
              <path d="M12 8V12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <path d="M12 16H12.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
            处理时间取决于文档长度和模型响应速度，可能需要几十秒
          </p>
          <el-alert
            v-if="processingError"
            :title="processingError"
            type="error"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
          />
          <div class="step-actions">
            <el-button @click="goBackToUpload" :disabled="processing">上一步</el-button>
            <el-button
              type="primary"
              :loading="processing"
              @click="doProcess"
            >
              {{ processing ? '处理中...' : (processingError ? '重试' : '开始处理') }}
            </el-button>
          </div>
        </div>
      </template>

      <!-- Step 3: 下载 -->
      <template v-else>
        <div class="step-card success-card fade-in">
          <div class="card-icon success-icon">
            <!-- SVG: 成功图标 -->
            <svg width="56" height="56" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>
              <path d="M8 12L11 15L16 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <h3>处理完成</h3>
          <p class="card-desc">下载处理结果，批改任务可同时查看下方问题清单</p>

          <!-- 批改任务问题清单卡片：点击卡片定位原文高亮，点击高亮定位卡片 -->
          <template v-if="gradingIssues">
            <div v-if="gradingDegraded" class="grading-degraded-tip">
              <el-alert
                title="模型未按约定格式返回，已退回自由文本结果"
                type="warning"
                :closable="false"
              />
            </div>
            <div v-else-if="!gradingIssues.length" class="grading-empty-tip">
              批改完成，未发现问题
            </div>

            <el-collapse v-else name="issues">
              <el-collapse-item :title="'批注清单（共 ' + gradingIssues.length + ' 处）'" name="issues">
                <div class="issue-list">
                  <div
                    v-for="(issue, idx) in gradingIssues"
                    :key="idx"
                    :id="'issue-anchor-' + idx"
                    :ref="'issueCard' + idx"
                    class="issue-card"
                    :class="{ 'issue-card-active': activeIssueIndex === idx }"
                    @click="onIssueClick(idx)"
                  >
                    <div class="issue-card-header">
                      <el-tag size="small" :color="TYPE_COLOR[issue.type]" effect="dark">
                        {{ TYPE_LABEL[issue.type] }}
                      </el-tag>
                      <span class="issue-confidence">把握 {{ Math.round(issue.confidence * 100) }}%</span>
                      <span v-if="issue.start === null" class="issue-unlocated">未定位</span>
                    </div>
                    <div class="issue-original">{{ issue.original }}</div>
                    <div v-if="issue.suggestion" class="issue-suggestion">→ {{ issue.suggestion }}</div>
                    <div v-if="issue.reason" class="issue-reason">{{ issue.reason }}</div>
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>

            <!-- 原文高亮视图 -->
            <el-collapse name="annotated">
              <el-collapse-item title="查看原文批注" name="annotated">
                <div class="text-preview annotated-text">
                    <template v-for="(para, pi) in displayTextParas" :key="pi">
                    <p>
                      <template v-for="(seg, si) in para.segs" :key="si">
                        <span
                          v-if="seg.issue"
                          class="highlight-segment"
                          :class="{ 'highlight-active': activeIssueIndex === seg.index }"
                          :style="{ backgroundColor: TYPE_COLOR[seg.issue.type] + '33' }"
                          @click="activeIssueIndex = seg.index"
                        >
                          {{ seg.text }}
                        </span>
                        <span v-else>{{ seg.text }}</span>
                      </template>
                    </p>
                  </template>
                </div>
              </el-collapse-item>
            </el-collapse>
          </template>

          <!-- 结果文本预览 -->
          <el-collapse v-if="processedContent" style="margin-bottom: 16px; text-align: left;">
            <el-collapse-item title="预览处理结果" name="result">
              <div class="text-preview">{{ processedContent }}</div>
            </el-collapse-item>
          </el-collapse>
          <div class="step-actions">
            <el-button @click="processNewFile">处理新文件</el-button>
            <el-button type="success" size="large" :loading="downloading" @click="doDownload">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 4px;">
                <path d="M21 15V19C21 19.5304 20.7893 20.0391 20.4142 20.4142C20.0391 20.7893 19.5304 21 19 21H5C4.46957 21 3.96086 20.7893 3.58579 20.4142C3.21071 20.0391 3 19.5304 3 19V15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M7 10L12 15L17 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M12 15V3" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              下载结果 .docx
            </el-button>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 步骤进度条容器 */
.steps-wrapper {
  margin-bottom: var(--space-xl);
  padding: var(--space-lg);
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  border-radius: 8px;
  border: 1px solid var(--sidebar-border);
}

/* 步骤卡片主体 */
.step-body {
  margin-top: var(--space-xl);
  display: flex;
  justify-content: center;
}

.step-card {
  width: 100%;
  max-width: 560px;
  padding: var(--space-xl);
  background: #ffffff;
  border-radius: 12px;
  border: 1px solid var(--sidebar-border);
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
  transition: all 0.3s ease;
  text-align: center;
}

.step-card:hover {
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
  transform: translateY(-2px);
}

/* 卡片顶部图标 */
.card-icon {
  width: 80px;
  height: 80px;
  margin: 0 auto var(--space-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: linear-gradient(135deg, #e6f4ff 0%, #f0f9ff 100%);
  color: #409eff;
  transition: all 0.3s ease;
}

.step-card:hover .card-icon {
  transform: scale(1.05);
}

.upload-icon {
  background: linear-gradient(135deg, #e6f7ff 0%, #f0f9ff 100%);
  color: #1890ff;
}

.process-icon {
  background: linear-gradient(135deg, #fff7e6 0%, #fffbe6 100%);
  color: #fa8c16;
}

.success-icon {
  background: linear-gradient(135deg, #f6ffed 0%, #fcffe6 100%);
  color: #52c41a;
  width: 96px;
  height: 96px;
}

/* 卡片标题和描述 */
.step-card h3 {
  margin: 0 0 var(--space-sm);
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

.card-desc {
  margin: 0 0 var(--space-lg);
  color: #606266;
  font-size: 14px;
  line-height: 1.6;
}

/* 上传区域 */
.upload-area {
  margin-bottom: var(--space-md);
}

.upload-area :deep(.el-upload-dragger) {
  padding: var(--space-xl);
  border-radius: 8px;
  border: 2px dashed #dcdfe6;
  transition: all 0.3s ease;
}

.upload-area :deep(.el-upload-dragger:hover) {
  border-color: #409eff;
  background: #f5f9ff;
}

/* 已选文件 */
.picked-file {
  margin-top: var(--space-md);
  padding: var(--space-sm) var(--space-md);
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 14px;
  color: #606266;
  display: inline-block;
}

.picked-file code {
  font-family: 'Consolas', 'Courier New', monospace;
  color: #409eff;
  font-weight: 500;
}

.file-size {
  color: #909399;
  font-size: 13px;
}

/* 提示信息 */
.hint {
  margin: 0 0 var(--space-md);
  color: #606266;
  font-size: 14px;
  line-height: 1.6;
}

.hint.warning {
  color: #e6a23c;
  padding: var(--space-sm) var(--space-md);
  background: #fffbe6;
  border-radius: 6px;
  border-left: 3px solid #faad14;
  text-align: left;
}

.empty-hint {
  margin-top: var(--space-md);
  color: #909399;
  font-size: 13px;
}

/* 文本预览区域（提取文本 / 处理结果） */
.text-preview {
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-md);
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'Consolas', 'Courier New', monospace;
}

/* 操作按钮区 */
.step-actions {
  margin-top: var(--space-lg);
  display: flex;
  justify-content: center;
  gap: var(--space-md);
}

/* 成功卡片 */
.success-card {
  background: linear-gradient(135deg, #f6ffed 0%, #fcffe6 100%);
  border-color: #b7eb8f;
}

.success-card:hover {
  background: linear-gradient(135deg, #f6ffed 0%, #fcffe6 100%);
}

/* 淡入动画 */
/* fadeInUp 已提取到全局 style.css，此处保留占位注释以说明来源 */
.fade-in {
  animation: fadeInUp 0.4s ease-out;
}

/* 响应式 */
@media (max-width: 768px) {
  .step-card {
    padding: var(--space-lg);
  }
  
  .card-icon {
    width: 64px;
    height: 64px;
  }
  
  .success-icon {
    width: 80px;
    height: 80px;
  }
}
</style>
