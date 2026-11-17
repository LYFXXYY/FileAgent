<script setup lang="ts">
/**
 * 模型提供商管理页
 *
 * 【设计说明】
 * - 后端返回的 api_key 已脱敏（前 4 位 + ****），编辑时不能直接用这个值回显
 * - 编辑场景：api_key 输入框留空表示"不改动"，有值才更新
 */
import { onActivated, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { providerApi } from '@/api/providers'
import type { Capability, Provider, ProviderCreatePayload } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const providers = ref<Provider[]>([])
const loading = ref(false)
const submitting = ref(false) // 防止对话框「保存」按钮重复点击

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()

// 能力候选值。后端加新能力时要同步改 schemas/provider.py 的 KNOWN_CAPABILITIES
const capabilityOptions: Capability[] = ['text', 'vision', 'embedding']

/**
 * 后端的逗号串 → 多选框要的数组
 *
 * 用候选值去反查而不是直接 split，顺带做了两件事：顺序归一化、丢掉库里的未知值，
 * 脏数据不至于把表单卡死。全不命中时兜底成 text。
 */
function toCapabilityList(raw: string): Capability[] {
  const picked = capabilityOptions.filter((item) => raw.split(',').includes(item))
  return picked.length ? picked : ['text']
}

const form = reactive<
  ProviderCreatePayload & { api_key: string; capabilityList: Capability[]; is_public: boolean }
>({
  name: '',
  base_url: '',
  api_key: '',
  model_name: '',
  capabilityList: ['text'],
  max_tokens: 4096,
  temperature: 0.7,
  is_public: false,
})

const rules: FormRules = {
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  base_url: [{ required: true, message: '请输入 API 基础地址', trigger: 'blur' }],
  model_name: [{ required: true, message: '请输入模型标识', trigger: 'blur' }],
  capabilityList: [{ required: true, message: '至少选择一种能力', trigger: 'change' }],
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

/** 表单 → 请求体：能力数组拍平成后端的逗号串，不带只给多选框用的临时字段 */
function buildPayload(): ProviderCreatePayload {
  return {
    name: form.name,
    base_url: form.base_url,
    api_key: form.api_key,
    model_name: form.model_name,
    capabilities: form.capabilityList.join(','),
    max_tokens: form.max_tokens,
    temperature: form.temperature,
    // 公共标记只有 admin 能改；普通用户提交恒为 false（后端也会拒 true）
    is_public: auth.isAdmin ? form.is_public : false,
  }
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
onActivated(load)

function openCreate() {
  dialogMode.value = 'create'
  editingId.value = null
  Object.assign(form, {
    name: '',
    base_url: '',
    api_key: '',
    model_name: '',
    capabilityList: ['text'],
    max_tokens: 4096,
    temperature: 0.7,
    is_public: false,
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
    capabilityList: toCapabilityList(row.capabilities),
    max_tokens: row.max_tokens,
    temperature: row.temperature,
    is_public: row.is_public,
  })
  dialogVisible.value = true
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    if (dialogMode.value === 'create') {
      await providerApi.create(buildPayload())
      ElMessage.success('提供商已创建')
    } else if (editingId.value !== null) {
      // 只传前端实际填了的字段，api_key 空则不传
      const payload: Record<string, unknown> = { ...buildPayload() }
      if (!payload.api_key) delete payload.api_key
      await providerApi.update(editingId.value, payload)
      ElMessage.success('提供商已更新')
    }
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    submitting.value = false
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
      <div class="page-header-content">
        <div class="page-icon">
          <!-- 模型提供商图标：钥匙 -->
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M21 2L19 4M11.3891 13.3912C9.82881 14.9515 7.30015 14.9515 5.73985 13.3912C4.17956 11.8309 4.17956 9.30224 5.73985 7.74195C7.30015 6.18165 9.82881 6.18165 11.3891 7.74195L19 2M19 2L22 5L18 9L15 6L19 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div>
          <h2 class="page-title">模型提供商</h2>
          <p class="page-subtitle">配置 OpenAI 兼容的 LLM 服务，支持豆包、通义千问、GPT 等</p>
        </div>
      </div>
      <el-button type="primary" class="add-btn" @click="openCreate">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="vertical-align: middle; margin-right: 4px;">
          <path d="M12 5V19M5 12H19" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        新建提供商
      </el-button>
    </div>

    <!-- 表格卡片包裹 -->
    <div class="table-wrapper">
      <el-table v-loading="loading" :data="providers" stripe>
        <el-table-column prop="name" label="名称" min-width="120">
          <template #default="{ row }">
            <span class="provider-name">{{ row.name }}</span>
            <el-tag v-if="row.is_public" type="danger" size="small" effect="plain" class="cap-tag">
              官方
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="base_url" label="API 地址" min-width="240" show-overflow-tooltip />
        <el-table-column prop="model_name" label="模型" min-width="140">
          <template #default="{ row }">
            <span class="model-tag">{{ row.model_name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="capabilities" label="能力" width="150">
          <template #default="{ row }">
            <el-tag
              v-for="item in toCapabilityList(row.capabilities)"
              :key="item"
              size="small"
              class="cap-tag"
            >
              {{ item }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="api_key_masked" label="API Key" width="140" />
        <el-table-column label="温度" width="80">
          <template #default="{ row }">{{ row.temperature.toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <!-- 别人名下的公共提供商：只读展示，启用/编辑/删除归 owner（admin） -->
            <el-switch
              v-if="auth.isAdmin || !row.is_public"
              v-model="row.is_active"
              @change="toggleActive(row as Provider)"
            />
            <span v-else class="model-tag">由管理员维护</span>
          </template>
        </el-table-column>
        <el-table-column v-if="auth.isAdmin || providers.some((p) => !p.is_public)" label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <template v-if="auth.isAdmin || !row.is_public">
              <el-button link type="primary" @click="openEdit(row as Provider)">编辑</el-button>
              <el-button link type="danger" @click="remove(row as Provider)">删除</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建/编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建提供商' : '编辑提供商'"
      width="600px"
      class="provider-dialog"
      @closed="formRef?.clearValidate()"
    >
      <div class="dialog-title-bar">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M21 2L19 4M11.3891 13.3912C9.82881 14.9515 7.30015 14.9515 5.73985 13.3912C4.17956 11.8309 4.17956 9.30224 5.73985 7.74195C7.30015 6.18165 9.82881 6.18165 11.3891 7.74195L19 2M19 2L22 5L18 9L15 6L19 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>{{ dialogMode === 'create' ? '添加新的模型提供商' : '修改提供商配置' }}</span>
      </div>
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
        <el-form-item label="模型能力" prop="capabilityList">
          <el-select v-model="form.capabilityList" multiple class="cap-select">
            <el-option v-for="item in capabilityOptions" :key="item" :label="item" :value="item" />
          </el-select>
          <div class="form-hint">
            text 处理文档 / vision 看截图做批评 / embedding 做向量检索。
            用途不匹配时后端会直接报错，不会偷偷换别的模型
          </div>
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
        <!-- 公共（官方默认）标记：仅 admin 可见可改，后端同样有守卫 -->
        <el-form-item v-if="auth.isAdmin" label="官方默认">
          <el-checkbox v-model="form.is_public">标记为公共默认模型（被授权用户可直接使用）</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 页面头部：图标 + 标题，与 Tasks.vue 保持一致 */
.page-header-content {
  display: flex;
  align-items: center;
  gap: var(--space-md);
}

.page-icon {
  width: 48px;
  height: 48px;
  background: linear-gradient(135deg, #6366f1 0%, #a78bfa 100%);
  color: #ffffff;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(99, 102, 241, 0.25);
}

/* 新建按钮渐变 */
.add-btn {
  background: linear-gradient(135deg, #6366f1 0%, #818cf8 100%);
  border: none;
  transition: all 0.3s ease;
}

.add-btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(99, 102, 241, 0.35);
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

/* 行悬停效果 */
:deep(.el-table .el-table__row) {
  transition: all 0.2s ease;
}

:deep(.el-table .el-table__row:hover > td) {
  background: #f8faff !important;
}

/* 提供商名称 */
.provider-name {
  font-weight: 500;
  color: #303133;
}

/* 模型标签 */
.model-tag {
  display: inline-block;
  padding: 2px 8px;
  background: #f0f9ff;
  color: #409eff;
  border-radius: 4px;
  font-size: 13px;
  font-family: 'Consolas', 'Courier New', monospace;
}

/* 能力标签：表格里并排显示，留间距防粘一起 */
.cap-tag {
  margin-right: 4px;
}

/* 能力多选框占满表单列宽 */
.cap-select {
  width: 100%;
}

/* 表单项下方的浅色说明 */
.form-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: #909399;
}

/* 对话框标题栏 */
.dialog-title-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: var(--space-sm) var(--space-md);
  margin-bottom: var(--space-md);
  background: linear-gradient(135deg, #f0f0ff 0%, #e8e0ff 100%);
  border-radius: 8px;
  color: #6366f1;
  font-weight: 500;
  font-size: 14px;
}

/* 对话框标题渐变 */
:deep(.provider-dialog .el-dialog__header) {
  border-bottom: 1px solid #f0f0f5;
  padding-bottom: 12px;
}

/* 淡入动画 */
/* fadeInUp 已提取到全局 style.css，此处保留占位注释以说明来源 */

/* 响应式 */
@media (max-width: 768px) {
  .page-icon {
    width: 40px;
    height: 40px;
  }

  .add-btn {
    font-size: 13px;
  }
}
</style>
