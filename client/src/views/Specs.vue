<script setup lang="ts">
/**
 * 规范库页（阶段 6）—— 上传参考规范、查看已入库的条款数、删除
 *
 * 【这一页解决什么】
 * 批改时后端会从"你自己的规范库"里按相关度召回条款，连同条款号一起进 prompt。
 * 规范库是用户私有的（后端每条查询都带 user_id），所以这里只看得见自己传的。
 *
 * 【为什么上传会失败】
 * 入库时同步做向量编码。编码端点不可用时整份回滚并报 400 —— 规范库没有"原地更新"
 * 的口子，留下"文本进了、向量缺了"的块就补不回来，检索会静默跳过它们。
 */
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { specApi } from '@/api/specs'
import type { SpecDoc } from '@/api/types'
import TableEmpty from '@/components/TableEmpty.vue'

const items = ref<SpecDoc[]>([])
const loading = ref(false)
const uploading = ref(false)
const titleDraft = ref('')

// 与工作台同一份白名单：后端 TEXT_EXTRACTORS 加格式时这里要跟着改
const ACCEPT = '.docx,.pdf'

async function load() {
  loading.value = true
  try {
    const resp = await specApi.list()
    items.value = resp.data
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

/**
 * el-upload 走 http-request 自定义上传：
 * 直接用它的默认请求会绕过 axios 拦截器，token 就带不上
 */
async function doUpload(option: { file: File }) {
  uploading.value = true
  try {
    const resp = await specApi.upload(option.file, titleDraft.value.trim())
    items.value = [resp.data, ...items.value]
    titleDraft.value = ''
    ElMessage.success(`已入库：${resp.data.title}（${resp.data.chunk_count} 条条款）`)
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    uploading.value = false
  }
}

async function removeDoc(row: SpecDoc) {
  try {
    await ElMessageBox.confirm(
      `删除「${row.title}」后，批改时不再召回它的 ${row.chunk_count} 条条款。要重新用需重传。`,
      '删除规范文档',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await specApi.remove(row.id)
    items.value = items.value.filter((i) => i.id !== row.id)
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div class="page-header-content">
        <div class="page-icon">
          <!-- 规范库：一本立着的书 -->
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
            <path
              d="M4 4.5A1.5 1.5 0 0 1 5.5 3H19v18H5.5A1.5 1.5 0 0 1 4 19.5V4.5Z"
              stroke="currentColor"
              stroke-width="2"
              stroke-linejoin="round"
            />
            <path d="M8 3v18" stroke="currentColor" stroke-width="2" />
            <path d="M11.5 8.5h4.5M11.5 12h4.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </div>
        <div>
          <h2 class="page-title">规范库</h2>
          <p class="page-subtitle">
            上传你的校对规范、格式要求或单位手册；批改时按相关度自动召回条款，并带条款号可溯源
          </p>
        </div>
      </div>
    </div>

    <!-- 上传区：与工作台同一套 accept，真校验在后端白名单 -->
    <div class="upload-row">
      <el-input
        v-model="titleDraft"
        placeholder="给这份规范起个名字（留空用文件名）"
        maxlength="255"
        class="title-input"
      />
      <el-upload
        :accept="ACCEPT"
        :show-file-list="false"
        :http-request="doUpload"
        :disabled="uploading"
      >
        <el-button type="primary" :loading="uploading">
          {{ uploading ? '入库并编码中…' : '上传规范文档' }}
        </el-button>
      </el-upload>
    </div>

    <p class="format-hint">
      支持 {{ ACCEPT }}；扫描件（无文字层）不做 OCR，表格不进正文。
      同名重传是新增一份，不覆盖旧的。
    </p>

    <div class="table-wrapper">
      <el-table v-loading="loading" :data="items" stripe>
        <template #empty>
          <TableEmpty
            title="规范库还是空的"
            hint="上传一份校对规范，之后的批改就会按你自己的标准判，而不是模型的通用直觉"
            action-text="刷新列表"
            @action="load"
          />
        </template>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
        <el-table-column prop="source_format" label="格式" width="90" />
        <el-table-column label="字数" width="110">
          <template #default="{ row }">{{ row.char_count.toLocaleString('zh-CN') }}</template>
        </el-table-column>
        <el-table-column label="条款数" width="110">
          <template #default="{ row }">{{ row.chunk_count }}</template>
        </el-table-column>
        <el-table-column label="入库时间" width="180">
          <template #default="{ row }">{{ formatDate(row.uploaded_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" align="center">
          <template #default="{ row }">
            <el-button type="danger" size="small" link @click="removeDoc(row as SpecDoc)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
/* 页面头部与表格卡片沿用全局 .page-* / .table-wrapper 约定（Logs.vue 同一套） */
.upload-row {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
  margin-bottom: var(--space-sm);
}

.title-input {
  max-width: 360px;
}

.format-hint {
  margin: 0 0 var(--space-md);
  font-size: var(--fa-text-xs);
  color: var(--el-text-color-secondary);
}

.table-wrapper {
  background: var(--fa-surface);
  border-radius: var(--fa-radius-card);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 7%, transparent);
  box-shadow: var(--fa-shadow-lg);
  overflow: hidden;
  animation: fadeInUp var(--fa-duration-enter) var(--fa-ease-spring) both;
}

:deep(.el-table th.el-table__cell) {
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
}
</style>
