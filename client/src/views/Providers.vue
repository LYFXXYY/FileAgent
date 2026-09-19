<script setup lang="ts">
/**
 * 模型提供商管理页
 *
 * 【设计说明】
 * - 后端返回的 api_key 已脱敏（前 4 位 + ****），编辑时不能直接用这个值回显
 * - 编辑场景：api_key 输入框留空表示"不改动"，有值才更新
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { providerApi } from '@/api/providers'
import type { Provider, ProviderCreatePayload } from '@/api/types'

const providers = ref<Provider[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()

const form = reactive<ProviderCreatePayload & { api_key: string }>({
  name: '',
  base_url: '',
  api_key: '',
  model_name: '',
  max_tokens: 4096,
  temperature: 0.7,
})

const rules: FormRules = {
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  base_url: [{ required: true, message: '请输入 API 基础地址', trigger: 'blur' }],
  model_name: [{ required: true, message: '请输入模型标识', trigger: 'blur' }],
  // 编辑时 api_key 可留空（表示不改）
  api_key: [
    {
      validator: (_rule, value: string, callback) => {
        if (dialogMode.value === 'create' && !value) {
          callback(new Error('请输入 API Key'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

async function load() {
  loading.value = true
  try {
    const resp = await providerApi.list()
    providers.value = resp.data
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
    base_url: '',
    api_key: '',
    model_name: '',
    max_tokens: 4096,
    temperature: 0.7,
  })
  dialogVisible.value = true
}

function openEdit(row: Provider) {
  dialogMode.value = 'edit'
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    base_url: row.base_url,
    api_key: '', // 留空表示不改
    model_name: row.model_name,
    max_tokens: row.max_tokens,
    temperature: row.temperature,
  })
  dialogVisible.value = true
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  try {
    if (dialogMode.value === 'create') {
      await providerApi.create(form)
      ElMessage.success('提供商已创建')
    } else if (editingId.value !== null) {
      // 只传前端实际填了的字段，api_key 空则不传
      const payload: Record<string, unknown> = { ...form }
      if (!payload.api_key) delete payload.api_key
      await providerApi.update(editingId.value, payload)
      ElMessage.success('提供商已更新')
    }
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
}

async function remove(row: Provider) {
  try {
    await ElMessageBox.confirm(`确定删除提供商 "${row.name}"？`, '提示', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await providerApi.remove(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (err) {
    if (err !== 'cancel' && err !== 'close') {
      ElMessage.error(extractErrorMessage(err))
    }
  }
}

async function toggleActive(row: Provider) {
  try {
    await providerApi.update(row.id, { is_active: row.is_active })
    ElMessage.success(row.is_active ? '已启用' : '已停用')
  } catch (err) {
    // 失败时回滚 switch 状态
    row.is_active = !row.is_active
    ElMessage.error(extractErrorMessage(err))
  }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">模型提供商</h2>
        <p class="page-subtitle">配置 OpenAI 兼容的 LLM 服务，支持豆包、通义千问、GPT 等</p>
      </div>
      <el-button type="primary" @click="openCreate">新建提供商</el-button>
    </div>

    <el-table v-loading="loading" :data="providers" border stripe>
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="base_url" label="API 地址" min-width="240" show-overflow-tooltip />
      <el-table-column prop="model_name" label="模型" min-width="140" />
      <el-table-column prop="api_key_masked" label="API Key" width="140" />
      <el-table-column label="温度" width="80">
        <template #default="{ row }">{{ row.temperature.toFixed(2) }}</template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-switch v-model="row.is_active" @change="toggleActive(row)" />
        </template>
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
      :title="dialogMode === 'create' ? '新建提供商' : '编辑提供商'"
      width="600px"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="120px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="如：豆包-pro" />
        </el-form-item>
        <el-form-item label="API 基础地址" prop="base_url">
          <el-input v-model="form.base_url" placeholder="如：https://ark.cn-beijing.volces.com/api/v3" />
        </el-form-item>
        <el-form-item label="模型标识" prop="model_name">
          <el-input v-model="form.model_name" placeholder="如：doubao-pro-128k" />
        </el-form-item>
        <el-form-item :label="dialogMode === 'edit' ? 'API Key（可选）' : 'API Key'" prop="api_key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="dialogMode === 'edit' ? '留空表示不改动' : '请输入 API Key'"
          />
        </el-form-item>
        <el-form-item label="Max Tokens">
          <el-input-number v-model="form.max_tokens" :min="1" :max="200000" />
        </el-form-item>
        <el-form-item label="Temperature">
          <el-slider v-model="form.temperature" :min="0" :max="2" :step="0.1" show-input />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
