<script setup lang="ts">
/**
 * 批改结果视图：问题清单 ↔ 原文高亮的双向联动
 *
 * 从 Home.vue 抽出来的理由：
 * 1. 这块的联动状态（activeIssueIndex）本来就是局部状态，不该占工作台的编排逻辑；
 * 2. 原先 .issue-card / .highlight-segment 一个样式规则都没有，清单一直是裸 HTML 渲染。
 *
 * 输入只有三样：清单、是否降级、原文。不碰 store、不发请求。
 */
import { computed, ref } from 'vue'

import type { GradingIssue } from '@/api/types'
import { buildParagraphs, TYPE_COLOR, TYPE_LABEL } from '@/utils/annotate'

const props = defineProps<{
  issues: GradingIssue[]
  degraded: boolean
  original: string
}>()

const activeIssueIndex = ref<number | null>(null)

const paras = computed(() => buildParagraphs(props.original, props.issues))

// 点卡片：选中并滚到原文里的对应位置
function selectIssue(idx: number) {
  activeIssueIndex.value = idx
  document
    .getElementById(`issue-anchor-${idx}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
</script>

<template>
  <div class="grading-result">
    <!-- 降级：模型没守格式，退回自由文本。必须和"批了但 0 条"分开说（AGENTS §3.7） -->
    <el-alert
      v-if="degraded"
      class="grading-alert"
      title="模型未按约定格式返回，已退回自由文本结果"
      type="warning"
      :closable="false"
      show-icon
    />

    <div v-else-if="!issues.length" class="grading-empty-tip">
      未发现可定位的文字问题
      <span class="grading-empty-note">
        表格与图片内容不进正文、扫描件不做 OCR——这部分不在批改范围内
      </span>
    </div>

    <template v-else>
      <el-collapse name="issues">
        <el-collapse-item :title="'批注清单（共 ' + issues.length + ' 处）'" name="issues">
          <div class="issue-list">
            <button
              v-for="(issue, idx) in issues"
              :id="'issue-anchor-' + idx"
              :key="idx"
              type="button"
              class="issue-card"
              :class="{ 'issue-card-active': activeIssueIndex === idx }"
              @click="selectIssue(idx)"
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
            </button>
          </div>
        </el-collapse-item>
      </el-collapse>

      <el-collapse name="annotated">
        <el-collapse-item title="查看原文批注" name="annotated">
          <div class="annotated-text">
            <p v-for="(para, pi) in paras" :key="pi">
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
          </div>
        </el-collapse-item>
      </el-collapse>
    </template>
  </div>
</template>

<style scoped>
.grading-result {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
  text-align: left;
}

.grading-alert {
  border-radius: var(--fa-radius-field);
}

/* 0 问题：只声明"没发现可定位的文字问题"，不替模型保证文档没问题 */
.grading-empty-tip {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: var(--space-md);
  font-size: var(--fa-text-base);
  color: var(--el-text-color-primary);
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
  border: 1px solid color-mix(in oklab, var(--fa-ink) 7%, transparent);
  border-left: 3px solid var(--fa-accent-pop);
  border-radius: var(--fa-radius-field);
}

.grading-empty-note {
  font-size: var(--fa-text-xs);
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

/* 清单：纸片堆叠感，选中那张靠暖橙左笔 + 阴影抬高 */
.issue-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 4px 0 8px;
}

.issue-card {
  display: block;
  width: 100%;
  padding: var(--space-md);
  text-align: left;
  font: inherit;
  color: var(--el-text-color-regular);
  cursor: pointer;
  background: var(--fa-surface);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
  border-left: 3px solid transparent;
  border-radius: var(--fa-radius-field);
  transition:
    border-color var(--fa-duration-fast) var(--fa-ease-spring),
    box-shadow var(--fa-duration-fast) var(--fa-ease-spring),
    transform var(--fa-duration-fast) var(--fa-ease-spring);
}

.issue-card:hover {
  box-shadow: var(--fa-shadow-lg);
  transform: translateY(-1px);
}

.issue-card-active {
  border-left-color: var(--fa-accent-pop);
  background: color-mix(in oklab, var(--fa-accent) 8%, var(--fa-surface));
  box-shadow: var(--fa-shadow-lg);
}

.issue-card-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-bottom: 8px;
}

.issue-confidence {
  font-size: var(--fa-text-xs);
  color: var(--el-text-color-secondary);
}

.issue-unlocated {
  padding: 1px 6px;
  font-size: var(--fa-text-xs);
  color: var(--el-text-color-secondary);
  background: color-mix(in oklab, var(--fa-ink) 5%, var(--fa-bg));
  border-radius: 3px;
}

.issue-original {
  font-size: var(--fa-text-base);
  color: var(--el-text-color-primary);
  word-break: break-word;
}

.issue-suggestion {
  margin-top: 4px;
  font-size: var(--fa-text-base);
  color: var(--fa-ink);
  font-weight: 500;
  word-break: break-word;
}

.issue-reason {
  margin-top: 4px;
  font-size: var(--fa-text-xs);
  color: var(--el-text-color-secondary);
}

/* 原文批注：延续 .text-preview 的等宽纸面感 */
.annotated-text {
  max-height: 320px;
  overflow-y: auto;
  padding: var(--space-md);
  font-size: var(--fa-text-sm);
  line-height: 1.9;
  color: var(--el-text-color-primary);
  background: color-mix(in oklab, var(--fa-ink) 3%, var(--fa-bg));
  border-radius: var(--fa-radius-field);
}

.annotated-text p {
  margin: 0 0 0.6em;
  white-space: pre-wrap;
  word-break: break-word;
}

.highlight-segment {
  cursor: pointer;
  border-radius: 3px;
  padding: 1px 2px;
  transition:
    box-shadow var(--fa-duration-fast) ease-out,
    background-color var(--fa-duration-fast) ease-out;
}

/* 选中片段加一圈描边，光靠底色在 8 种类型里区分度不够 */
.highlight-active {
  box-shadow: 0 0 0 2px color-mix(in oklab, var(--fa-ink) 55%, transparent);
}
</style>
