<script setup lang="ts">
/**
 * 任务管理页 —— 列表 + 新增/编辑对话框 + 删除
 *
 * 【Vue 常见模式】
 * - v-model:dialogVisible 用 Element Plus 的双向绑定语法
 * - template #default="{ row }" 是插槽语法，从 el-table-column 拿到当前行数据
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { providerApi } from '@/api/providers'
import { taskApi } from '@/api/tasks'
import type { Provider, Task, TaskCreatePayload } from '@/api/types'

const tasks = ref<Task[]>([])
const providers = ref<Provider[]>([])
const loading = ref(false)

// 对话框状态
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()

const form = reactive<TaskCreatePayload>({
  name: '',
  description: '',
  prompt_template: '',
  is_grading: false,
  provider_id: null,
})

const rules: FormRules = {
  name: [{ required: true, message: '请输入任务名称', trigger: 'blur' }],
  prompt_template: [{ required: true, message: '请输入 Prompt 模板', trigger: 'blur' }],
}

async function load() {
  loading.value = true
  try {
    const [tasksResp, providersResp] = await Promise.all([
      taskApi.list(),
      providerApi.list(),
    ])
    tasks.value = tasksResp.data
    providers.value = providersResp.data
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openCreate() {
  dialogMode.value = 'create'
  editingId.value = null
  Object.assign(form, {
    name: '',
    description: '',
    prompt_template: '',
    is_grading: false,
    provider_id: null,
  })
  dialogVisible.value = true
}

function openEdit(row: Task) {
  dialogMode.value = 'edit'
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    description: row.description ?? '',
    prompt_template: row.prompt_template,
    is_grading: row.is_grading,
    provider_id: row.provider_id,
  })
  dialogVisible.value = true
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  try {
    if (dialogMode.value === 'create') {
      await taskApi.create(form)
      ElMessage.success('任务已创建')
    } else if (editingId.value !== null) {
      await taskApi.update(editingId.value, form)
      ElMessage.success('任务已更新')
    }
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
}

async function remove(row: Task) {
  try {
    await ElMessageBox.confirm(`确定删除任务 "${row.name}"？`, '提示', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await taskApi.remove(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (err) {
    // 用户取消会 reject 到这里，忽略即可
    if (err !== 'cancel' && err !== 'close') {
      ElMessage.error(extractErrorMessage(err))
    }
  }
}

function providerName(id: number | null): string {
  if (id === null) return '（默认）'
  return providers.value.find((p) => p.id === id)?.name ?? '已删除'
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">任务管理</h2>
        <p class="page-subtitle">定义 Prompt 模板，一个任务对应一种处理流程</p>
      </div>
      <el-button type="primary" @click="openCreate">新建任务</el-button>
    </div>

    <el-table v-loading="loading" :data="tasks" border stripe>
      <el-table-column prop="name" label="任务名称" min-width="140" />
      <el-table-column prop="description" label="描述" min-width="180" show-overflow-tooltip />
      <el-table-column label="Prompt 模板" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <code class="prompt-preview">{{ row.prompt_template.slice(0, 40) }}...</code>
        </template>
      </el-table-column>
      <el-table-column label="批改" width="80" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.is_grading" type="warning" size="small">是</el-tag>
          <span v-else class="text-muted">否</span>
        </template>
      </el-table-column>
      <el-table-column label="使用模型" width="140">
        <template #default="{ row }">{{ providerName(row.provider_id) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建任务' : '编辑任务'"
      width="600px"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="120px">
        <el-form-item label="任务名称" prop="name">
          <el-input v-model="form.name" placeholder="如：作文批改" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="2"
            placeholder="可选，给用户看的说明"
          />
        </el-form-item>
        <el-form-item label="Prompt 模板" prop="prompt_template">
          <el-input
            v-model="form.prompt_template"
            type="textarea"
            :rows="6"
            placeholder="用 {{ '{{content}}' }} 作为文档内容占位符"
          />
        </el-form-item>
        <el-form-item label="使用模型">
          <el-select v-model="form.provider_id" clearable placeholder="默认使用第一个启用的提供商" style="width: 100%">
            <el-option
              v-for="p in providers"
              :key="p.id"
              :label="`${p.name} (${p.model_name})`"
              :value="p.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="批改类任务">
          <el-switch v-model="form.is_grading" />
          <span class="text-muted" style="margin-left: 8px">
            开启后下载结果会用红字标注 diff
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.prompt-preview {
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 12px;
  color: #606266;
}

.text-muted {
  color: #909399;
  font-size: 13px;
}
</style>
