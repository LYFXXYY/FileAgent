<script setup lang="ts">
/**
 * 操作日志页 —— 分页表格
 *
 * 【Vue 概念】
 * - 表格里的日期用 toLocaleString 直接格式化，学习项目不用引入 date-fns
 * - el-pagination 的 current-change / size-change 事件驱动重新加载
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { logsApi } from '@/api/logs'
import type { LogEntry } from '@/api/types'

const items = ref<LogEntry[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const statusType = {
  pending: 'info',
  processing: 'primary',
  done: 'success',
  failed: 'danger',
} as const

async function load() {
  loading.value = true
  try {
    const resp = await logsApi.list(page.value, pageSize.value)
    items.value = resp.data.items
    total.value = resp.data.total
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(load)

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
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">操作日志</h2>
        <p class="page-subtitle">你的每一次上传、处理、下载记录</p>
      </div>
    </div>

    <el-table v-loading="loading" :data="items" border stripe>
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="original_filename" label="文件" min-width="180" show-overflow-tooltip />
      <el-table-column prop="task_name" label="任务" width="140" />
      <el-table-column prop="provider_name" label="模型" width="120">
        <template #default="{ row }">
          {{ row.provider_name ?? '—' }}
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
    </el-table>

    <el-pagination
      style="margin-top: 16px; justify-content: flex-end"
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
.error-text {
  color: #f56c6c;
  font-size: 13px;
}

.text-muted {
  color: #c0c4cc;
}
</style>
