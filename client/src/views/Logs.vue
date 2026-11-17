<script setup lang="ts">
/**
 * 操作日志页 —— 分页表格 + 搜索筛选 + 重试 + 空状态
 *
 * 【功能】
 * - 按状态筛选（全部 / pending / processing / done / failed）
 * - 按文件名模糊搜索
 * - 失败记录可一键重试（重新调用 LLM 处理）
 * - 无数据时显示空状态提示
 */
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { logsApi } from '@/api/logs'
import { processApi } from '@/api/process'
import type { LogEntry } from '@/api/types'

const items = ref<LogEntry[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

// 搜索筛选
const filterStatus = ref<string>('') // '' 表示全部
const filterFilename = ref<string>('')
const retryingId = ref<number | null>(null) // 正在重试的行 ID

const statusType = {
  pending: 'info',
  processing: 'primary',
  done: 'success',
  failed: 'danger',
} as const

const statusOptions = [
  { label: '全部', value: '' },
  { label: '待处理', value: 'pending' },
  { label: '处理中', value: 'processing' },
  { label: '已完成', value: 'done' },
  { label: '已失败', value: 'failed' },
]

async function load() {
  loading.value = true
  try {
    const resp = await logsApi.list(
      page.value,
      pageSize.value,
      filterStatus.value || undefined,
      filterFilename.value || undefined,
    )
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(load)
onActivated(load)

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}

function onPageChange(p: number) {
  page.value = p
  load()
}

function onSizeChange(size: number) {
  pageSize.value = size
  page.value = 1
  load()
}

// 搜索：重置到第一页再加载
function onSearch() {
  page.value = 1
  load()
}

// 重置筛选条件
function onResetFilters() {
  filterStatus.value = ''
  filterFilename.value = ''
  page.value = 1
  load()
}

// 下载处理结果
async function downloadLog(row: LogEntry) {
  if (row.status !== 'done') return
  try {
    const resp = await processApi.download(row.id)
    const url = URL.createObjectURL(resp.data)
    const a = document.createElement('a')
    a.href = url
    a.download = `result_${row.original_filename}`
    a.click()
    URL.revokeObjectURL(url)
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
}

// 判断是否可下载
function canDownload(status: string): boolean {
  return status === 'done'
}

// 重试失败的处理
async function retryProcess(row: LogEntry) {
  try {
    await ElMessageBox.confirm(
      `确定要重新处理文件「${row.original_filename}」吗？`,
      '重试处理',
      { type: 'warning', confirmButtonText: '重试', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }

  retryingId.value = row.id
  try {
    const resp = await processApi.process(row.id)
    if (resp.data.status === 'done') {
      ElMessage.success('处理完成')
      await load() // 刷新列表
    } else if (resp.data.status === 'failed') {
      ElMessage.error(resp.data.error_message ?? '处理失败')
      await load()
    }
    // processing 状态不轮询（日志页非工作台），用户稍后刷新即可
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    retryingId.value = null
  }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div class="page-header-content">
        <div class="page-icon">
          <!-- 操作日志图标：时钟 -->
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>
            <path d="M12 6V12L16 14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div>
          <h2 class="page-title">操作日志</h2>
          <p class="page-subtitle">你的每一次上传、处理、下载记录</p>
        </div>
      </div>
    </div>

    <!-- 搜索筛选栏 -->
    <div class="filter-bar">
      <el-select
        v-model="filterStatus"
        placeholder="状态筛选"
        clearable
        style="width: 140px"
        @change="onSearch"
      >
        <el-option
          v-for="opt in statusOptions"
          :key="opt.value"
          :label="opt.label"
          :value="opt.value"
        />
      </el-select>
      <el-input
        v-model="filterFilename"
        placeholder="按文件名搜索..."
        clearable
        style="width: 240px"
        @keyup.enter="onSearch"
        @clear="onSearch"
      />
      <el-button type="primary" @click="onSearch">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 4px;">
          <circle cx="11" cy="11" r="8" stroke="currentColor" stroke-width="2"/>
          <path d="M21 21L16.65 16.65" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
        搜索
      </el-button>
      <el-button @click="onResetFilters">重置</el-button>
    </div>

    <div class="table-wrapper">
      <el-table v-loading="loading" :data="items" stripe>
        <!-- 空状态 -->
        <template #empty>
          <el-empty description="暂无操作记录" :image-size="120">
            <template v-if="filterStatus || filterFilename">
              <p class="empty-tip">没有匹配筛选条件的记录</p>
              <el-button type="primary" size="small" @click="onResetFilters">清除筛选</el-button>
            </template>
            <template v-else>
              <p class="empty-tip">去工作台上传文件开始处理吧</p>
              <el-button type="primary" size="small" @click="$router.push('/')">前往工作台</el-button>
            </template>
          </el-empty>
        </template>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="original_filename" label="文件" min-width="180" show-overflow-tooltip />
        <el-table-column prop="task_name" label="任务" width="140" />
        <el-table-column prop="provider_name" label="模型" width="120">
          <template #default="{ row }">
            <span class="model-name">{{ row.provider_name ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="180">
          <template #default="{ row }">{{ formatDate(row.uploaded_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType[row.status as keyof typeof statusType]" size="small">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="错误信息" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.error_message" class="error-text">{{ row.error_message }}</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" align="center">
          <template #default="{ row }">
            <el-button
              v-if="canDownload(row.status)"
              type="primary"
              size="small"
              @click="downloadLog(row as LogEntry)"
            >
              下载
            </el-button>
            <el-button
              v-else-if="row.status === 'failed'"
              type="warning"
              size="small"
              :loading="retryingId === row.id"
              @click="retryProcess(row as LogEntry)"
            >
              重试
            </el-button>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-pagination
      class="logs-pagination"
      background
      layout="total, sizes, prev, pager, next"
      :total="total"
      :current-page="page"
      :page-size="pageSize"
      :page-sizes="[10, 20, 50, 100]"
      @current-change="onPageChange"
      @size-change="onSizeChange"
    />
  </div>
</template>

<style scoped>
/* 页面头部：图标 + 标题 */
.page-header-content {
  display: flex;
  align-items: center;
  gap: var(--space-md);
}

.page-icon {
  width: 48px;
  height: 48px;
  background: linear-gradient(135deg, #e6a23c 0%, #f0c78a 100%);
  color: #ffffff;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(230, 162, 60, 0.25);
}

/* 搜索筛选栏 */
.filter-bar {
  display: flex;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
  align-items: center;
}

/* 表格卡片包裹：圆角 + 阴影 + 入场动画 */
.table-wrapper {
  background: #ffffff;
  border-radius: 12px;
  border: 1px solid #e4e7ed;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
  overflow: hidden;
  animation: fadeInUp 0.4s ease-out;
}

/* 表头渐变背景 */
:deep(.el-table th.el-table__cell) {
  background: linear-gradient(135deg, #f5f7fa 0%, #eef2ff 100%);
}

/* 模型名称标签风格 */
.model-name {
  display: inline-block;
  padding: 2px 8px;
  background: #f0f9ff;
  color: #409eff;
  border-radius: 4px;
  font-size: 13px;
  font-family: 'Consolas', 'Courier New', monospace;
}

.error-text {
  color: #f56c6c;
  font-size: 13px;
}

.text-muted {
  color: #c0c4cc;
}

/* 空状态提示文字 */
.empty-tip {
  color: #909399;
  font-size: 13px;
  margin: 0 0 8px;
}

/* 分页器 */
.logs-pagination {
  margin-top: 16px;
  justify-content: flex-end;
}

/* 响应式 */
@media (max-width: 768px) {
  .page-icon {
    width: 40px;
    height: 40px;
  }

  .filter-bar {
    flex-wrap: wrap;
  }
}
</style>
