/**
 * 结构化批改结果的纯逻辑：问题类型配色/文案 + 原文分段高亮
 *
 * 原先这三样埋在 Home.vue 的 <script setup> 里，改一行就得整页回归，
 * 也没法单测。抽出来之后 Home.vue 只负责流程编排，渲染细节归组件。
 *
 * 类型是开放输入 + 后端归一化（services/grading.py:normalize_type），
 * 前端只认这 8 个 canonical 值，与 schemas/grading.py 的 ISSUE_TYPES 一一对应。
 */

import type { GradingIssue } from '@/api/types'

/**
 * 问题类型的数据编码色板。
 *
 * ⚠ 这是**分类数据**不是装饰：八色用来让用户一眼区分"错别字"和"结构问题"，
 * 不要为了跟暖白/墨黑主色系统一而压成单色阶梯。
 */
export const TYPE_COLOR: Record<string, string> = {
  typo: '#dc2626', // 红
  grammar: '#ea580c', // 橙
  wording: '#d97706', // 黄
  punctuation: '#65a30d', // 黄绿
  structure: '#0891b2', // 蓝绿
  logic: '#4f46e5', // 靛
  content: '#7c3aed', // 紫
  other: '#64748b', // 灰
}

export const TYPE_LABEL: Record<string, string> = {
  typo: '错别字',
  grammar: '语法',
  wording: '用词',
  punctuation: '标点',
  structure: '结构',
  logic: '逻辑',
  content: '内容',
  other: '其它',
}

export interface Span {
  text: string
  issue?: GradingIssue
  /** 对应 issues 数组下标；非高亮片段必须是 null（不能留 undefined，否则点击时写进 activeIndex 永不匹配） */
  index: number | null
}

export interface Para {
  segs: Span[]
}

/**
 * 把原文按 `\n` 切成段，再按全局偏移 start/end 把问题片段套进 span
 *
 * 权威副本约定：原文一行一段（docx 与 PDF 提取结果同构），所以这里的偏移
 * 计算直接依赖"段长 + 1"累加。后端匹配不到的片段（start 为 null）保持不高亮，
 * 由界面显示"未定位"——绝不猜位置。
 */
export function buildParagraphs(text: string, issues: GradingIssue[] | null): Para[] {
  if (!text) return []
  if (!issues) {
    return text.split('\n').map((line) => ({ segs: [{ text: line, index: null }] }))
  }

  const lines = text.split('\n')
  const offsets: number[] = []
  lines.reduce((acc, l) => {
    offsets.push(acc)
    return acc + l.length + 1
  }, 0)

  const out: Para[] = []
  for (const [i, line] of lines.entries()) {
    const start = offsets[i]
    const end = start + line.length
    const marks = issues
      .map((issue, idx) => ({ issue, idx }))
      .filter(
        ({ issue }) =>
          issue.start !== null &&
          issue.end !== null &&
          issue.start! < end &&
          issue.end! > start,
      )
      .sort((a, b) => a.issue.start! - b.issue.start!)
      .map(({ issue, idx }) => ({
        s: Math.max(0, issue.start! - start),
        e: Math.min(line.length, issue.end! - start),
        issue,
        idx,
      }))

    const segs: Span[] = []
    let last = 0
    for (const m of marks) {
      if (m.s > last) segs.push({ text: line.slice(last, m.s), index: null })
      segs.push({ text: line.slice(m.s, m.e), issue: m.issue, index: m.idx })
      last = m.e
    }
    if (last < line.length) segs.push({ text: line.slice(last), index: null })
    out.push({ segs })
  }
  return out
}
