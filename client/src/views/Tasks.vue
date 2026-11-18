<script setup lang="ts">
/**
 * 任务管理页 —— 列表 + 新增/编辑对话框 + 删除
 *
 * 【Vue 常见模式】
 * - v-model:dialogVisible 用 Element Plus 的双向绑定语法
 * - template #default="{ row }" 是插槽语法，从 el-table-column 拿到当前行数据
 */
import { onActivated, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'

import { extractErrorMessage } from '@/api/client'
import { providerApi } from '@/api/providers'
import { taskApi } from '@/api/tasks'
import type { OutputFormat, Provider, Task, TaskCreatePayload } from '@/api/types'

/**
 * 预置的常用任务模板（纯前端静态常量，不会写入数据库）
 *
 * 【设计意图】
 * - 用户在「快速模板」下拉里一键选用，选中后自动填充表单的
 *   name / description / prompt_template / is_grading，仍可继续手动修改
 * - description 是开发者预设的只读「任务说明」，让用户一眼看懂该任务能做什么
 * - prompt_template 中的 {{content}} 是占位符，后端处理时会替换为文档正文
 */
interface PresetTask {
  name: string
  description: string
  prompt_template: string
  is_grading: boolean
}

const PRESET_TASKS: PresetTask[] = [
  {
    name: '作文批改',
    description:
      '将学生作文交给 AI 逐句批改，指出错别字、语法、用词与结构问题，并给出修改建议与总评；下载结果时会对改动处做红字标注。',
    prompt_template:
      '你是一位资深语文老师，请批改下面的学生作文。要求：\n1. 指出错别字与标点错误；\n2. 指出语法和用词不当之处；\n3. 分析文章结构是否清晰；\n4. 给出总体评价与具体修改建议。\n\n作文内容：\n{{content}}',
    is_grading: true,
  },
  {
    name: '文本润色',
    description:
      '在不改变原文意思和语气的前提下，优化措辞与句式，让表达更流畅、更专业、更自然。',
    prompt_template:
      '请对以下文本进行润色，使表达更流畅、更专业、更自然。要求保留原文的核心意思、语气和结构，只优化措辞与句式。\n\n原文：\n{{content}}',
    is_grading: false,
  },
  {
    name: '内容摘要',
    description: '提炼文档的核心观点与关键信息，生成一份结构清晰、重点突出的内容摘要。',
    prompt_template:
      '请阅读以下文档，提炼核心观点与关键信息，生成一份结构清晰、重点突出、逻辑通顺的内容摘要。\n\n文档内容：\n{{content}}',
    is_grading: false,
  },
  {
    name: '中英翻译',
    description: '将文档内容翻译成英文，保持表达准确、专业、通顺，并尽量保留原文的段落与结构。',
    prompt_template:
      '请将以下中文文档翻译成英文。要求表达准确、专业、通顺，并尽量保留原文的段落结构。\n\n中文原文：\n{{content}}',
    is_grading: false,
  },
  {
    name: '关键词提取',
    description: '从文档中提取最重要的关键词与主题词，帮助快速把握文档核心。',
    prompt_template:
      '请从以下文档中提取最重要的关键词与主题词，按重要程度排序并简要说明每个关键词的含义。\n\n文档内容：\n{{content}}',
    is_grading: false,
  },
]

const tasks = ref<Task[]>([])
const providers = ref<Provider[]>([])
const loading = ref(false)
const submitting = ref(false) // 防止对话框「保存」按钮重复点击

// 对话框状态
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref<number | null>(null)
const formRef = ref<FormInstance>()

// 「快速模板」下拉当前选中的下标（PRESET_TASKS 数组索引），未选中为 null
const selectedTemplate = ref<number | null>(null)

// 任务说明是否处于「草稿」可编辑状态：默认只读，点「复制为草稿」后变为可编辑
const descriptionEditable = ref(false)

const form = reactive<TaskCreatePayload>({
  name: '',
  description: '',
  prompt_template: '',
  is_grading: false,
  provider_id: null,
  output_format: 'docx',
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
onActivated(load)

function openCreate() {
  dialogMode.value = 'create'
  editingId.value = null
  Object.assign(form, {
    name: '',
    description: '',
    prompt_template: '',
    is_grading: false,
    provider_id: null,
    output_format: 'docx' as OutputFormat,
  })
  // 新建时清空「快速模板」选择，并让任务说明回到只读状态
  selectedTemplate.value = null
  descriptionEditable.value = false
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
    output_format: row.output_format,
  })
  // 编辑时同样清空「快速模板」高亮，避免误触模板后覆盖已有内容
  selectedTemplate.value = null
  descriptionEditable.value = false
  dialogVisible.value = true
}

/**
 * 「快速模板」下拉选择事件：选中后一键填充表单
 *
 * 【类型注意】
 * - el-select 开启 clearable 后，清空时 change 事件可能回传 '' / null / undefined，
 *   这里把入参放宽成 number | string | null | undefined 统一处理
 * @param index PRESET_TASKS 的下标（选中）或清空标记（空值）
 */
function onTemplateChange(index: number | string | null | undefined) {
  if (index === null || index === undefined || index === '') return
  const tpl = PRESET_TASKS[index as number]
  if (!tpl) return

  // 一键填充：名称 + 只读说明 + Prompt 模板 + 是否批改
  form.name = tpl.name
  form.description = tpl.description
  form.prompt_template = tpl.prompt_template
  form.is_grading = tpl.is_grading
  // 换模板后把说明切回「只读」，避免用户误改开发者预设内容
  descriptionEditable.value = false
}

/** 「复制为草稿」：把只读的任务说明切换成可编辑的草稿 */
function copyDescriptionToDraft() {
  // 说明文本本来就在 form.description 里，直接放开编辑即可，无需额外草稿变量
  descriptionEditable.value = true
}

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
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
  } finally {
    submitting.value = false
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
      <div class="page-header-content">
        <!-- 页面图标 -->
        <div class="page-icon">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M9 5H7C5.89543 5 5 5.89543 5 7V19C5 20.1046 5.89543 21 7 21H17C18.1046 21 19 20.1046 19 19V7C19 5.89543 18.1046 5 17 5H15" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <rect x="9" y="3" width="6" height="4" rx="1" stroke="currentColor" stroke-width="2"/>
            <path d="M9 12H15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <path d="M9 16H13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </div>
        <div>
          <h2 class="page-title">任务管理</h2>
          <p class="page-subtitle">定义 Prompt 模板，一个任务对应一种处理流程</p>
        </div>
      </div>
      <el-button type="primary" class="create-btn" @click="openCreate">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="margin-right: 4px;">
          <path d="M12 5V19" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          <path d="M5 12H19" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
        新建任务
      </el-button>
    </div>

    <!-- 任务列表表格 -->
    <div class="table-wrapper">
      <el-table v-loading="loading" :data="tasks" class="task-table">
        <el-table-column prop="name" label="任务名称" min-width="140">
          <template #default="{ row }">
            <span class="task-name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="180" show-overflow-tooltip />
        <el-table-column label="Prompt 模板" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <code class="prompt-preview">{{ row.prompt_template.slice(0, 40) }}{{ row.prompt_template.length > 40 ? '...' : '' }}</code>
          </template>
        </el-table-column>
        <el-table-column label="批改" width="80" align="center">
          <template #default="{ row }">
            <!-- 属性标记用主色，success/warning/danger 留给状态列 -->
            <el-tag v-if="row.is_grading" type="primary" size="small" effect="light">是</el-tag>
            <span v-else class="text-muted">否</span>
          </template>
        </el-table-column>
        <el-table-column label="结果格式" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.output_format === 'pptx'" type="primary" size="small" effect="plain">
              .pptx
            </el-tag>
            <span v-else class="text-muted">.docx</span>
          </template>
        </el-table-column>
        <el-table-column label="使用模型" width="140">
          <template #default="{ row }">
            <span class="model-name">{{ providerName(row.provider_id) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" class="action-btn" @click="openEdit(row as Task)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="margin-right: 2px;">
                <path d="M11 4H4C3.46957 4 2.96086 4.21071 2.58579 4.58579C2.21071 4.96086 2 5.46957 2 6V20C2 20.5304 2.21071 21.0391 2.58579 21.4142C2.96086 21.7893 3.46957 22 4 22H18C18.5304 22 19.0391 21.7893 19.4142 21.4142C19.7893 21.0391 20 20.5304 20 20V13" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M18.5 2.50001C18.8978 2.10219 19.4374 1.87869 20 1.87869C20.5626 1.87869 21.1022 2.10219 21.5 2.50001C21.8978 2.89784 22.1213 3.43739 22.1213 4.00001C22.1213 4.56262 21.8978 5.10219 21.5 5.50001L12 15L8 16L9 12L18.5 2.50001Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              编辑
            </el-button>
            <el-button link type="danger" class="action-btn delete-btn" @click="remove(row as Task)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="margin-right: 2px;">
                <path d="M3 6H5H21" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M8 6V4C8 3.46957 8.21071 2.96086 8.58579 2.58579C8.96086 2.21071 9.46957 2 10 2H14C14.5304 2 15.0391 2.21071 15.4142 2.58579C15.7893 2.96086 16 3.46957 16 4V6M19 6V20C19 20.5304 18.7893 21.0391 18.4142 21.4142C18.0391 21.7893 17.5304 22 17 22H7C6.46957 22 5.96086 21.7893 5.58579 21.4142C5.21071 21.0391 5 20.5304 5 20V6H19Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建任务' : '编辑任务'"
      width="600px"
      @closed="formRef?.clearValidate()"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="120px">
        <!-- 快速模板下拉：一键填充，是「简化创建流程」的核心入口 -->
        <el-form-item label="快速模板">
          <el-select
            v-model="selectedTemplate"
            placeholder="选择一个常用模板，快速填充表单"
            clearable
            style="width: 100%"
            @change="onTemplateChange"
          >
            <el-option
              v-for="(tpl, idx) in PRESET_TASKS"
              :key="tpl.name"
              :label="tpl.name"
              :value="idx"
            />
          </el-select>
          <p class="field-hint">选中后自动填充名称、任务说明与 Prompt 模板，之后仍可继续修改。</p>
        </el-form-item>

        <el-form-item label="任务名称" prop="name">
          <el-input v-model="form.name" placeholder="如：作文批改" />
        </el-form-item>

        <el-form-item label="任务说明">
          <!-- 只读说明：默认不可编辑；点「复制为草稿」后才放开 -->
          <div class="description-wrap">
            <el-input
              v-model="form.description"
              type="textarea"
              :rows="3"
              :readonly="!descriptionEditable"
              placeholder="从快速模板带出，或点击「复制为草稿」后自行填写"
            />
            <el-button
              v-if="!descriptionEditable"
              class="draft-btn"
              link
              type="primary"
              @click="copyDescriptionToDraft"
            >
              复制为草稿
            </el-button>
          </div>
          <p class="field-hint">任务说明为开发者预设的只读说明；如需自定义，请点击「复制为草稿」后修改。</p>
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
        <el-form-item label="结果格式">
          <el-radio-group v-model="form.output_format">
            <el-radio value="docx">Word (.docx)</el-radio>
            <el-radio value="pptx">PPT (.pptx)</el-radio>
          </el-radio-group>
          <span class="text-muted" style="margin-left: 8px">
            PPT 走阶段 5 的"大纲 → 拼装 → 视觉批评"图，目前仅文字型 PDF 输入
          </span>
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
/* 页面头部的 .page-header-content / .page-icon 已提到全局 style.css，五页共用 */

/* 新建按钮：外观交给主题层的 primary 渐变胶囊，这里只加粗 */
.create-btn {
  font-weight: 600;
}

/* ========== 表格容器 ========== */
/* 卡片式表格包裹：圆角 + 墨色阴影 + 悬停加深 */
.table-wrapper {
  background: var(--fa-surface);
  border-radius: 16px;
  border: 1px solid color-mix(in oklab, var(--fa-ink) 7%, transparent);
  box-shadow: var(--fa-shadow-lg);
  overflow: hidden;
  transition: box-shadow var(--fa-duration-fast) var(--fa-ease-spring);
  animation: fadeInUp 0.35s var(--fa-ease-spring) both;
}

.table-wrapper:hover {
  box-shadow: var(--fa-shadow-xl);
}

/* 表格行悬停高亮：暖橙荧光笔扫过一行 */
:deep(.el-table__row:hover > td) {
  background: color-mix(in oklab, var(--fa-accent) 9%, var(--fa-surface));
}

/* 表格头部：暖中性填充 */
:deep(.el-table th.el-table__cell) {
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
  font-weight: 600;
  color: var(--el-text-color-primary);
  border-bottom: 2px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
}

/* 去掉表格最外层边框，让 table-wrapper 的圆角生效 */
:deep(.el-table::before) {
  display: none;
}

/* ========== 表格单元格 ========== */
/* 任务名称：加粗，悬停点亮暖橙 */
.task-name {
  font-weight: 600;
  color: var(--el-text-color-primary);
  transition: color var(--fa-duration-fast) ease-out;
}

:deep(.el-table__row:hover) .task-name {
  color: var(--fa-accent);
}

/* 模型名称：标签风格 */
.model-name {
  font-size: 13px;
  color: var(--el-text-color-regular);
  padding: 2px 8px;
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
  border-radius: var(--fa-radius-field);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
}

/* Prompt 预览代码块 */
.prompt-preview {
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 12px;
  color: var(--el-text-color-regular);
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
  padding: 2px 6px;
  border-radius: 4px;
}

.text-muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

/* ========== 操作按钮 ========== */
.action-btn {
  transition: transform var(--fa-duration-fast) var(--fa-ease-spring);
}

.action-btn:hover {
  transform: translateY(-1px);
}

.delete-btn:hover {
  text-shadow: 0 1px 4px rgba(245, 108, 108, 0.3);
}

/* ========== 对话框 ========== */
:deep(.el-dialog__header) {
  padding-bottom: 12px;
  border-bottom: 1px solid color-mix(in oklab, var(--fa-ink) 6%, transparent);
  margin-right: 0;
}

:deep(.el-dialog__title) {
  font-weight: 600;
  font-size: 18px;
  color: var(--el-text-color-primary);
}

:deep(.el-dialog__body) {
  padding-top: 20px;
}

:deep(.el-dialog__footer) {
  border-top: 1px solid color-mix(in oklab, var(--fa-ink) 6%, transparent);
  padding-top: 16px;
}

/* ========== 表单内部 ========== */
/* 任务说明区域 */
.description-wrap {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.draft-btn {
  margin-top: 6px;
  font-size: 12px;
}

/* 表单控件下方的辅助提示文字 */
.field-hint {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: #909399;
}

/* 输入框焦点高亮 */
:deep(.el-input__wrapper:focus-within),
:deep(.el-textarea__inner:focus) {
  box-shadow: 0 0 0 1px var(--fa-ink) inset !important;
}

/* ========== 动画 ========== */
/* fadeInUp 已提取到全局 style.css，此处保留占位注释以说明来源 */
</style>
