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
 * - 等待上限是墙钟（store 里的 pollDeadlineAt），切页和刷新都不重置它；
 *   到点后前端只停手，出口是"继续等待"或去看操作日志，绝不是重新提交处理
 */
import { computed, onActivated, onDeactivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { processApi } from '@/api/process'
import { providerApi } from '@/api/providers'
import { taskApi } from '@/api/tasks'
import type { ChunkProgress, GradingIssue, Provider, SpecRef, Task } from '@/api/types'
import GradingResult from '@/components/GradingResult.vue'
import { useIsMobile } from '@/composables/useIsMobile'
import { useHomeStore } from '@/stores/home'
import { resolveFilename, saveBlob } from '@/utils/download'

const homeStore = useHomeStore()
const { isMobile } = useIsMobile()

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
const specRefs = ref<SpecRef[] | null>(homeStore.specRefs)
const specNote = ref<string | null>(homeStore.specNote)
// 长文档分块进度。刻意不进 store：它是瞬时状态，刷新后第一次轮询就会重新拿到，
// 持久化反而会显示上一次任务的块数，那是骗人
const chunkProgress = ref<ChunkProgress | null>(null)
const chunkPercent = computed(() => {
  const p = chunkProgress.value
  if (!p || p.total <= 0) return 0
  return Math.round((p.done / p.total) * 100)
})

// ---------- 结构化批改渲染 ----------
// 类型色板/文案与分段高亮的纯逻辑在 utils/annotate.ts，视图在 components/GradingResult.vue。
// 抽走的理由：联动状态本该局部于结果视图，且那两块原先没有任何样式规则。
const originalText = computed(() => uploadResult.value?.extracted_text || '')

// 可用模型提供商数量（为 0 时在选任务步骤提示用户先配置）
const activeProviderCount = computed(() =>
  providers.value.filter((p) => p.is_active).length
)

// 当前选中的任务：md/pptx 转换零模型，提示文案与下载按钮都要按它分流
const selectedTask = computed(
  () => tasks.value.find((t) => t.id === selectedTaskId.value) ?? null
)
const isConversionTask = computed(
  () => selectedTask.value?.output_format === 'md' || selectedTask.value?.output_format === 'pptx'
)

const downloadLabel = computed(() => {
  const fmt = selectedTask.value?.output_format
  if (fmt === 'md') return '下载 Markdown'
  if (fmt === 'pptx') return '下载 PPT (.pptx)'
  return '下载结果 .docx'
})

const processHint = computed(() => {
  if (selectedTask.value?.output_format === 'md') {
    return 'Word 本地转换很快；PDF 走 MinerU 版面识别，可能需要 1–3 分钟'
  }
  if (selectedTask.value?.output_format === 'pptx') {
    return 'PDF 走 MinerU 版面识别后本地拼装 PPT，可能需要 1–3 分钟；配置了视觉模型时还会修复坏块并校对版面'
  }
  if (uploadResult.value?.ocr_pending) {
    return '扫描件识别 + 模型处理，可能需要几分钟'
  }
  return '处理时间取决于文档长度和模型响应速度，可能需要几十秒'
})

// ---------- 轮询机制 ----------
// 处理中如果浏览器刷新或 axios 超时，后端可能仍在处理
// 通过定期查询 status 接口，完成后自动推进到下载步骤
const POLL_INTERVAL = 5000 // 5 秒轮询一次
// 等待预算走墙钟（store 里的 pollDeadlineAt），不是"轮询次数"。
// 上限必须大于后端最坏路径：MinerU 轮询上限 mineru_max_wait=600s，之后还要跑
// 坏块修复与版面批评两段 VLM 外呼。取 20 分钟，给版面循环留出至少两轮的时间。
const POLL_TIMEOUT_MS = 20 * 60 * 1000
// 文案里的分钟数跟着预算走，别写成第二个字面量
const POLL_TIMEOUT_MINUTES = Math.round(POLL_TIMEOUT_MS / 60000)
let pollingTimer: ReturnType<typeof setInterval> | null = null
// 只是"前端不再等了"，不代表后端失败——两者UI出口必须不同
const waitTimedOut = ref(false)

function startPolling() {
  stopPolling()
  if (homeStore.pollDeadlineAt === null) {
    homeStore.pollDeadlineAt = Date.now() + POLL_TIMEOUT_MS
  }
  // 切页回来或刷新时，截止时间可能已经过了。落回超时态，
  // 而不是再开一张永远到不了期的表——旧实现每次 startPolling 把计数清零，
  // 用户来回切页就能无限等，上限形同虚设。
  if (Date.now() > homeStore.pollDeadlineAt) {
    markWaitTimedOut()
    return
  }
  waitTimedOut.value = false
  pollingTimer = setInterval(pollOnce, POLL_INTERVAL)
}

function stopPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer)
    pollingTimer = null
  }
}

// 前端停止等待：不清 logId、不改状态机，后端那单还在跑
function markWaitTimedOut() {
  stopPolling()
  processing.value = false
  waitTimedOut.value = true
  syncToStore()
}

// 超时后唯一的出口应该是"接着查"，不是"重新提交"——后者会再烧一次
// MinerU 配额与模型 token，而文档本身没变。
function resumeWaiting() {
  homeStore.pollDeadlineAt = Date.now() + POLL_TIMEOUT_MS
  processing.value = true
  waitTimedOut.value = false
  startPolling()
  syncToStore()
}

async function pollOnce() {
  if (homeStore.pollDeadlineAt !== null && Date.now() > homeStore.pollDeadlineAt) {
    markWaitTimedOut()
    return
  }

  const logId = homeStore.logId
  if (!logId) return

  try {
    const resp = await processApi.status(logId)
    applyProgress(resp.data)
    if (resp.data.status === 'done') {
      homeStore.pollDeadlineAt = null
      stopPolling()
      currentStep.value = 3
      processing.value = false
      waitTimedOut.value = false
      applyResult(resp.data)
      ElMessage.success('处理完成')
      syncToStore()
    } else if (resp.data.status === 'failed') {
      homeStore.pollDeadlineAt = null
      stopPolling()
      processing.value = false
      waitTimedOut.value = false
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
  homeStore.specRefs = specRefs.value
  homeStore.specNote = specNote.value
}

// 把一次处理成功的响应内容铺到 UI：正文 + 问题清单 + 降级标记
function applyResult(resp: {
  processed_content: string | null
  issues: GradingIssue[] | null
  grading_degraded: boolean
  spec_refs?: SpecRef[] | null
  spec_note?: string | null
  original_content?: string | null
}) {
  processedContent.value = resp.processed_content
  gradingIssues.value = resp.issues
  gradingDegraded.value = resp.grading_degraded
  specRefs.value = resp.spec_refs ?? null
  specNote.value = resp.spec_note ?? null
  // 扫描件上传时没有预览文本，OCR 写回后由 status 响应补上原文，
  // 否则 GradingResult 的原文高亮没有底本。普通上传已有预览，不重复覆盖
  if (resp.original_content && uploadResult.value && !uploadResult.value.extracted_text) {
    uploadResult.value = {
      ...uploadResult.value,
      extracted_text: resp.original_content,
      content_length: resp.original_content.length,
      ocr_pending: false,
    }
  }
}

// 分块进度的唯一铺值入口（和 applyResult 同理：三处状态读取都要走它，别各自赋值）
function applyProgress(resp: { status: string; progress?: ChunkProgress | null }) {
  chunkProgress.value = resp.status === 'processing' ? resp.progress ?? null : null
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
  specRefs.value = homeStore.specRefs
  specNote.value = homeStore.specNote
  pickedFile.value = null // File 对象不可序列化，恢复时重置
  // 超时态不单独持久化：截止时间在 store 里，过期与否按当前时刻重算一次即可。
  // 漏了这步，刷新后会停在第 2 步——没有转圈、没有报错、也没有超时提示。
  waitTimedOut.value =
    homeStore.pollDeadlineAt !== null &&
    Date.now() > homeStore.pollDeadlineAt &&
    homeStore.processedContent === null

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
      applyProgress(resp.data)
      if (resp.data.status === 'done') {
        homeStore.pollDeadlineAt = null
        stopPolling()
        currentStep.value = 3
        processing.value = false
        waitTimedOut.value = false
        applyResult(resp.data)
        syncToStore()
      } else if (resp.data.status === 'failed') {
        homeStore.pollDeadlineAt = null
        stopPolling()
        processing.value = false
        waitTimedOut.value = false
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
  // 新一轮等待要有自己的截止时间，不能沿用上一轮剩下的（可能已过期）
  homeStore.pollDeadlineAt = null
  waitTimedOut.value = false
  // 上一轮的块数进度不能留到这一轮：换了文档切成几块都可能变了
  chunkProgress.value = null
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
      homeStore.pollDeadlineAt = null
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
    // 文件名以服务端 Content-Disposition 为准（批改_x.docx / 转换_x.pptx /
    // 转换_x.md 或 .zip，前缀与扩展名都在后端定）。自己拼 original_filename 会把
    // PDF 输入的结果命名成 .pdf 的 docx，md 任务更是连扩展名都对不上
    saveBlob(
      resp.data,
      resolveFilename(
        resp.headers['content-disposition'] as string | undefined,
        `result_${uploadResult.value.original_filename}`,
      ),
    )
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
  waitTimedOut.value = false
  homeStore.pollDeadlineAt = null
  chunkProgress.value = null
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
  waitTimedOut.value = false
  homeStore.pollDeadlineAt = null
  processedContent.value = null
  gradingIssues.value = null
  gradingDegraded.value = false
  specRefs.value = null
  specNote.value = null
  chunkProgress.value = null
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
  waitTimedOut.value = false
  processedContent.value = null
  gradingIssues.value = null
  gradingDegraded.value = false
  specRefs.value = null
  specNote.value = null
  chunkProgress.value = null
  homeStore.clear() // 清空 store 中的持久化状态（含 pollDeadlineAt）
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div class="page-header-content">
        <div class="page-icon">
          <!-- 工作台：房子图标，与导航入口一致 -->
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M3 9L12 2L21 9V20C21 21.1 20.1 22 19 22H5C3.9 22 3 21.1 3 20V9Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M9 22V12H15V22" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div>
          <h2 class="page-title">工作台</h2>
          <p class="page-subtitle">选择任务 → 上传文档 → LLM 处理 → 下载结果</p>
        </div>
      </div>
      <el-button v-if="currentStep > 0" @click="reset">重新开始</el-button>
    </div>

    <!-- 步骤进度条：自定义样式，增加图标和过渡动效 -->
    <div class="steps-wrapper">
      <el-steps :active="currentStep" finish-status="success" align-center>
        <el-step :title="isMobile ? '选择' : '选择任务'" />
        <el-step :title="isMobile ? '上传' : '上传文档'" />
        <el-step :title="isMobile ? '处理' : 'AI 处理'" />
        <el-step :title="isMobile ? '下载' : '下载结果'" />
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
            v-if="tasks.length > 0 && activeProviderCount === 0 && !isConversionTask"
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
          <div class="card-icon">
            <!-- SVG: 上传图标 -->
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M21 15V19C21 19.5304 20.7893 20.0391 20.4142 20.4142C20.0391 20.7893 19.5304 21 19 21H5C4.46957 21 3.96086 20.7893 3.58579 20.4142C3.21071 20.0391 3 19.5304 3 19V15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M17 8L12 3L7 8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M12 3V15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <h3>上传文档</h3>
          <p class="card-desc">支持 .docx / .pdf 文档，扫描件 PDF 处理时由 MinerU 识别（需服务端配置）</p>
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
          <!-- 开放给别人用之后的留存与外发告知（2026-11-22 拍板：上传页加一行说明）。
               说清三件事：文件存在哪、什么情况下会离开本机、以及别拿别人的隐私来试。
               这不只是礼貌——扫描件会整页送到 MinerU 第三方云按页识别。 -->
          <p class="hint upload-notice">
            上传的文档会保存在本服务器用于处理与下载。扫描件 PDF 会整页送入 MinerU
            云端识别后再批改；文字型 .docx / .pdf 在本机提取，不外发。请勿上传包含他人隐私或保密内容的文件。
          </p>
          <div v-if="pickedFile" class="picked-file">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 4px;">
              <path d="M14 2H6C5.46957 2 4.96086 2.21071 4.58579 2.58579C4.21071 2.96086 4 3.46957 4 4V20C4 20.5304 4.21071 21.0391 4.58579 21.4142C4.96086 21.7893 5.46957 22 6 22H18C18.5304 22 19.0391 21.7893 19.4142 21.4142C19.7893 21.0391 20 20.5304 20 20V8L14 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M14 2V8H20" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
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
          <div class="card-icon">
            <!-- SVG: AI 处理图标 -->
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M12 2L2 7L12 12L22 7L12 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M2 17L12 22L22 17" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M2 12L12 17L22 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <h3>{{ isConversionTask ? '转换处理' : '调用 LLM 处理' }}</h3>
          <p class="card-desc">
            文件 <code>{{ uploadResult?.original_filename }}</code> 已上传<template v-if="uploadResult?.ocr_pending">，属扫描件，正文将在处理时由 MinerU 识别</template><template v-else>，提取到 {{ uploadResult?.content_length }} 字符</template>
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
            {{ processHint }}
          </p>
          <el-alert
            v-if="processingError"
            :title="processingError"
            type="error"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
          />
          <div v-if="chunkProgress" class="chunk-progress">
            <p class="chunk-progress-label">
              长文档分块批改：已完成 {{ chunkProgress.done }} / {{ chunkProgress.total }} 块
            </p>
            <el-progress :percentage="chunkPercent" :stroke-width="8" :show-text="false" />
            <p class="hint">
              各块并行处理，进度按已批完的块数计。某一块失败后重跑，进度会回落到 0，
              但已成功的块不会重复调用模型、也不会重复花钱
            </p>
          </div>
          <!-- 超时不等于失败：后端那单可能还在跑。这里的出口只能是"接着查"
               或"去看日志"，绝不能挂 doProcess——那会重跑整条流水线，
               再烧一次 MinerU 配额和模型 token，而文档一个字没变。 -->
          <el-alert
            v-if="waitTimedOut"
            :title="`已等待 ${POLL_TIMEOUT_MINUTES} 分钟仍未拿到结果`"
            type="warning"
            description="文档可能仍在后台处理。继续等待只是重新查询状态，不会重新提交处理，也不会二次计费。"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
          />
          <div v-if="waitTimedOut" class="step-actions">
            <el-button @click="$router.push('/logs')">查看操作日志</el-button>
            <el-button type="primary" @click="resumeWaiting">继续等待</el-button>
          </div>
          <div v-else class="step-actions">
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

          <!-- 批改结果：清单 ↔ 原文高亮联动，视图与状态都在 GradingResult 内部 -->
          <GradingResult
            v-if="gradingIssues"
            :issues="gradingIssues"
            :degraded="gradingDegraded"
            :refs="specRefs"
            :note="specNote"
            :original="originalText"
          />

          <!-- 结果文本预览 -->
          <el-collapse v-if="processedContent" style="margin-bottom: 16px; text-align: left;">
            <el-collapse-item title="预览处理结果" name="result">
              <div class="text-preview">{{ processedContent }}</div>
            </el-collapse-item>
          </el-collapse>
          <div class="step-actions">
            <el-button @click="processNewFile">处理新文件</el-button>
            <el-button type="primary" size="large" :loading="downloading" @click="doDownload">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 4px;">
                <path d="M21 15V19C21 19.5304 20.7893 20.0391 20.4142 20.4142C20.0391 20.7893 19.5304 21 19 21H5C4.46957 21 3.96086 20.7893 3.58579 20.4142C3.21071 20.0391 3 19.5304 3 19V15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M7 10L12 15L17 10" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M12 15V3" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              {{ downloadLabel }}
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
  background: color-mix(in oklab, var(--fa-ink) 2%, var(--fa-bg));
  border-radius: var(--fa-radius-card);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 7%, transparent);
}

/* 步骤条：已完成=墨色对勾，进行中=暖橙呼吸圆圈（只染 icon 圆圈，head 是容器不能刷底） */
.steps-wrapper :deep(.el-step__head.is-success .el-step__icon.is-text) {
  background: var(--fa-ink);
  border-color: var(--fa-ink);
  color: #fff;
}

.steps-wrapper :deep(.el-step__head.is-process .el-step__icon.is-text) {
  background: var(--fa-accent-pop);
  border-color: var(--fa-accent-pop);
  color: #fff;
  animation: stepPulse 1.8s ease-in-out infinite;
}

.steps-wrapper :deep(.el-step__head.is-wait) {
  color: var(--el-text-color-placeholder);
  border-color: color-mix(in oklab, var(--fa-ink) 18%, transparent);
}

.steps-wrapper :deep(.el-step__title.is-process) {
  color: var(--fa-ink);
  font-weight: 600;
}

@keyframes stepPulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 color-mix(in oklab, var(--fa-accent-pop) 45%, transparent);
  }
  50% {
    box-shadow: 0 0 0 7px transparent;
  }
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
  background: var(--fa-surface);
  border-radius: var(--fa-radius-box);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 7%, transparent);
  box-shadow: var(--fa-shadow-lg);
  transition:
    box-shadow var(--fa-duration-fast) var(--fa-ease-spring),
    transform var(--fa-duration-fast) var(--fa-ease-spring);
  text-align: center;
}

.step-card:hover {
  box-shadow: var(--fa-shadow-xl);
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
  background: color-mix(in oklab, var(--fa-ink) 5%, var(--fa-bg));
  color: var(--fa-ink);
  transition: transform var(--fa-duration-fast) ease-out;
}

.step-card:hover .card-icon {
  animation: iconBob 1.8s ease-in-out infinite;
}

/* 图标轻微浮动：处理中/悬停时卡片是“活”的，不是死面板 */
@keyframes iconBob {
  0%,
  100% {
    transform: translateY(0) scale(1.05);
  }
  50% {
    transform: translateY(-5px) scale(1.05);
  }
}

/* 成功态：像红笔盖下的一个章，弹一下 */
@keyframes stampPop {
  0% {
    transform: scale(0.4);
    opacity: 0;
  }
  60% {
    transform: scale(1.12);
  }
  100% {
    transform: scale(1);
    opacity: 1;
  }
}

.success-icon {
  background: color-mix(in oklab, var(--el-color-success) 10%, var(--fa-bg));
  color: var(--el-color-success);
  width: 96px;
  height: 96px;
  animation: stampPop var(--fa-duration-enter) var(--fa-ease-spring) both;
}

/* 卡片标题和描述 */
.step-card h3 {
  margin: 0 0 var(--space-sm);
  font-size: var(--fa-text-lg);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.card-desc {
  margin: 0 0 var(--space-lg);
  color: var(--el-text-color-regular);
  font-size: var(--fa-text-base);
  line-height: 1.6;
}

/* 上传区域 */
.upload-area {
  margin-bottom: var(--space-md);
}

.upload-area :deep(.el-upload-dragger) {
  padding: var(--space-xl);
  border-radius: var(--fa-radius-box);
  border: 2px dashed color-mix(in oklab, var(--fa-ink) 16%, transparent);
  transition:
    border-color var(--fa-duration-fast) ease-out,
    background var(--fa-duration-fast) ease-out;
}

.upload-area :deep(.el-upload-dragger:hover) {
  border-color: color-mix(in oklab, var(--fa-ink) 40%, transparent);
  background: color-mix(in oklab, var(--fa-ink) 2%, var(--fa-bg));
}

/* 已选文件：做成贴在纸上的标签，而不是一块灰底 */
.picked-file {
  margin-top: var(--space-md);
  padding: var(--space-sm) var(--space-md);
  background: var(--fa-surface);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 10%, transparent);
  border-left: 3px solid var(--fa-accent-pop);
  border-radius: var(--fa-radius-field);
  box-shadow: 0 2px 8px color-mix(in oklab, var(--fa-ink) 5%, transparent);
  font-size: var(--fa-text-base);
  color: var(--el-text-color-regular);
  display: inline-block;
  text-align: left;
}

.picked-file code {
  font-family: 'Consolas', 'Courier New', monospace;
  color: var(--el-text-color-primary);
  font-weight: 500;
}

.file-size {
  color: var(--el-text-color-secondary);
  font-size: var(--fa-text-sm);
}

/* 提示信息 */
.hint {
  margin: 0 0 var(--space-md);
  color: var(--el-text-color-regular);
  font-size: var(--fa-text-base);
  line-height: 1.6;
}

.hint.warning {
  color: var(--el-color-warning);
  padding: var(--space-sm) var(--space-md);
  background: color-mix(in oklab, var(--el-color-warning) 8%, var(--fa-bg));
  border-radius: var(--fa-radius-field);
  border-left: 3px solid var(--el-color-warning);
  text-align: left;
}

/* 长文档分块进度：只在后端给到 progress 时出现 */
.chunk-progress {
  margin: 0 0 var(--space-md);
  padding: var(--space-sm) var(--space-md);
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
  border-radius: var(--fa-radius-field);
  border-left: 3px solid var(--fa-ink);
  text-align: left;
}

.chunk-progress-label {
  margin: 0 0 8px;
  font-size: var(--fa-text-base);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.chunk-progress .hint {
  margin: 8px 0 0;
  font-size: var(--fa-text-xs);
  color: var(--el-text-color-secondary);
}

.empty-hint {
  margin-top: var(--space-md);
  color: var(--el-text-color-secondary);
  font-size: var(--fa-text-sm);
}

/* 文本预览区域（提取文本 / 处理结果） */
.text-preview {
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-md);
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
  border-radius: var(--fa-radius-field);
  font-size: var(--fa-text-sm);
  line-height: 1.7;
  color: var(--el-text-color-primary);
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

/* 成功卡片：面底色不变，靠语义绿图标与按钮表达状态 */
.success-card {
  border-color: color-mix(in oklab, var(--el-color-success) 30%, transparent);
}

/* 淡入动画：带一点回弹的入场 */
.fade-in {
  animation: fadeInUp var(--fa-duration-enter) var(--fa-ease-spring) both;
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
