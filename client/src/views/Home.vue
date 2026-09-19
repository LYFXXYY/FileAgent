<script setup lang="ts">
/**
 * 工作台 —— 上传 → 处理 → 下载 三步流程
 *
 * 【设计思路】
 * 页面用 el-steps 展示进度，一次只显示当前步骤的操作控件
 * 这样用户视线聚焦、路径清晰，也避免同屏塞太多表单
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'

import { extractErrorMessage } from '@/api/client'
import { processApi } from '@/api/process'
import { taskApi } from '@/api/tasks'
import type { Task, UploadResponse } from '@/api/types'

// 步骤：0 选任务 → 1 上传 → 2 处理 → 3 完成可下载
const currentStep = ref(0)

// 各步状态
const tasks = ref<Task[]>([])
const selectedTaskId = ref<number | null>(null)
const pickedFile = ref<File | null>(null)
const uploadResult = ref<UploadResponse | null>(null)
const processing = ref(false)
const processingError = ref<string | null>(null)

// ---------- 生命周期 ----------
onMounted(async () => {
  try {
    const resp = await taskApi.list()
    tasks.value = resp.data
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
})

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

async function doUpload() {
  if (!selectedTaskId.value || !pickedFile.value) return
  try {
    const resp = await processApi.upload(selectedTaskId.value, pickedFile.value)
    uploadResult.value = resp.data
    currentStep.value = 2
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
}

// ---------- 步骤 3：处理 ----------
async function doProcess() {
  if (!uploadResult.value) return
  processing.value = true
  processingError.value = null
  try {
    const resp = await processApi.process(uploadResult.value.log_id)
    if (resp.data.status === 'done') {
      currentStep.value = 3
      ElMessage.success('处理完成')
    } else {
      processingError.value = resp.data.error_message ?? '处理失败'
      ElMessage.error(processingError.value)
    }
  } catch (err) {
    processingError.value = extractErrorMessage(err)
    ElMessage.error(processingError.value)
  } finally {
    processing.value = false
  }
}

// ---------- 步骤 4：下载 ----------
async function doDownload() {
  if (!uploadResult.value) return
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
  }
}

// ---------- 重置 ----------
function reset() {
  currentStep.value = 0
  selectedTaskId.value = null
  pickedFile.value = null
  uploadResult.value = null
  processingError.value = null
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">工作台</h2>
        <p class="page-subtitle">选择任务 → 上传 Word → LLM 处理 → 下载结果</p>
      </div>
      <el-button v-if="currentStep > 0" @click="reset">重新开始</el-button>
    </div>

    <el-steps :active="currentStep" finish-status="success" align-center>
      <el-step title="选择任务" />
      <el-step title="上传文档" />
      <el-step title="AI 处理" />
      <el-step title="下载结果" />
    </el-steps>

    <div class="step-body">
      <!-- Step 0: 选任务 -->
      <template v-if="currentStep === 0">
        <div class="step-card">
          <h3>选择处理任务</h3>
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
        </div>
      </template>

      <!-- Step 1: 上传文件 -->
      <template v-else-if="currentStep === 1">
        <div class="step-card">
          <h3>上传 Word 文档</h3>
          <el-upload
            drag
            accept=".docx"
            :auto-upload="false"
            :show-file-list="false"
            :on-change="onFileChange"
          >
            <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
            <div class="el-upload__text">
              拖拽 .docx 文件到此处，或 <em>点击选择</em>
            </div>
          </el-upload>
          <div v-if="pickedFile" class="picked-file">
            已选择：<code>{{ pickedFile.name }}</code>
            <span class="file-size">（{{ (pickedFile.size / 1024).toFixed(1) }} KB）</span>
          </div>
          <div class="step-actions">
            <el-button @click="currentStep = 0">上一步</el-button>
            <el-button
              type="primary"
              :disabled="!pickedFile"
              @click="doUpload"
            >
              上传
            </el-button>
          </div>
        </div>
      </template>

      <!-- Step 2: 处理 -->
      <template v-else-if="currentStep === 2">
        <div class="step-card">
          <h3>调用 LLM 处理</h3>
          <p class="hint">
            文件 <code>{{ uploadResult?.original_filename }}</code> 已上传，
            提取到 {{ uploadResult?.content_length }} 字符。
          </p>
          <p class="hint warning">处理时间取决于文档长度和模型响应速度，可能需要几十秒。</p>
          <el-alert
            v-if="processingError"
            :title="processingError"
            type="error"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
          />
          <div class="step-actions">
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
        <div class="step-card success-card">
          <h3>处理完成</h3>
          <p class="hint">点击下方按钮下载结果 Word 文件。</p>
          <div class="step-actions">
            <el-button type="success" size="large" @click="doDownload">
              下载结果 .docx
            </el-button>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.step-body {
  margin-top: var(--space-xl);
  display: flex;
  justify-content: center;
}

.step-card {
  width: 100%;
  max-width: 560px;
  padding: var(--space-lg);
  background: #fafbfc;
  border-radius: 6px;
  border: 1px solid var(--sidebar-border);
}

.step-card h3 {
  margin: 0 0 var(--space-md);
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}

.hint {
  margin: 0 0 var(--space-md);
  color: #606266;
  font-size: 14px;
}

.hint.warning {
  color: #e6a23c;
}

.empty-hint {
  margin-top: var(--space-md);
  color: #909399;
  font-size: 13px;
}

.picked-file {
  margin-top: var(--space-md);
  font-size: 14px;
  color: #606266;
}

.file-size {
  color: #909399;
}

.step-actions {
  margin-top: var(--space-lg);
  display: flex;
  justify-content: center;
  gap: var(--space-md);
}

.success-card {
  text-align: center;
  background: #f0f9eb;
  border-color: #b3e19d;
}
</style>
