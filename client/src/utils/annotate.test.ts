/**
 * 批注渲染纯逻辑测试
 *
 * 重点钉住 AGENTS.md §4 记过的两个易错点：
 * 1. 非高亮片段必须给 index: null（漏了会把 activeIssueIndex 写成 undefined，点击永不匹配）；
 * 2. 全局偏移要折算成段内偏移，且第二行以后才算得出真错——第一行是恒等偏移，测不出问题。
 */
import { describe, expect, it } from 'vitest'

import type { GradingIssue } from '@/api/types'
import { buildParagraphs, TYPE_COLOR, TYPE_LABEL } from './annotate'

function issue(over: Partial<GradingIssue> = {}): GradingIssue {
  return {
    type: 'typo',
    original: 'x',
    suggestion: 'y',
    reason: '',
    confidence: 0.9,
    start: 0,
    end: 1,
    ...over,
  }
}

describe('buildParagraphs', () => {
  it('空原文不产出段落', () => {
    expect(buildParagraphs('', [issue()])).toEqual([])
  })

  it('issues 为 null（非批改任务）时不套任何高亮', () => {
    const paras = buildParagraphs('第一段\n第二段', null)
    expect(paras).toHaveLength(2)
    expect(paras[0].segs).toEqual([{ text: '第一段', index: null }])
  })

  it('行内一条意见切成 前/高亮/后 三段', () => {
    const paras = buildParagraphs('他高兴的跳起来', [issue({ start: 1, end: 4 })])
    expect(paras[0].segs.map((s) => s.text)).toEqual(['他', '高兴的', '跳起来'])
    expect(paras[0].segs[1].index).toBe(0)
    // 两侧非高亮片段必须是 null，不能是 undefined
    expect(paras[0].segs[0].index).toBeNull()
    expect(paras[0].segs[2].index).toBeNull()
  })

  it('第二行的全局偏移要减去行首，否则区间整体偏短', () => {
    // "AAAA\nBBBB"：第二行起始偏移是 5
    const paras = buildParagraphs('AAAA\nBBBB', [issue({ start: 6, end: 8 })])
    expect(paras[0].segs).toEqual([{ text: 'AAAA', index: null }])
    expect(paras[1].segs.map((s) => s.text)).toEqual(['B', 'BB', 'B'])
    expect(paras[1].segs[1].index).toBe(0)
  })

  it('跨行意见在每一行各取自己的那一段，不越界', () => {
    // 全局 [1,5) 覆盖 "AA" + 第二行首字符 "B"（第二行起始偏移是 4）
    const paras = buildParagraphs('AAA\nBBB', [issue({ start: 1, end: 5 })])
    expect(paras[0].segs.map((s) => s.text)).toEqual(['A', 'AA'])
    expect(paras[0].segs.map((s) => s.index)).toEqual([null, 0])
    expect(paras[1].segs.map((s) => s.text)).toEqual(['B', 'BB'])
    expect(paras[1].segs.map((s) => s.index)).toEqual([0, null])
  })

  it('未定位（start 为 null）不产生高亮片段', () => {
    const paras = buildParagraphs('一句话', [issue({ start: null, end: null })])
    expect(paras[0].segs).toEqual([{ text: '一句话', index: null }])
  })

  it('同行两条按出现顺序排列，索引指向各自在清单里的下标', () => {
    const paras = buildParagraphs('ABCD', [
      issue({ start: 2, end: 4 }), // 后出现
      issue({ start: 0, end: 1 }), // 先出现
    ])
    const marked = paras[0].segs.filter((s) => s.issue)
    expect(marked.map((s) => s.index)).toEqual([1, 0])
    expect(marked.map((s) => s.text)).toEqual(['A', 'CD'])
  })
})

describe('类型色板', () => {
  it('8 个 canonical 类型都有配色与文案，且两份键集一致', () => {
    expect(Object.keys(TYPE_COLOR)).toEqual(Object.keys(TYPE_LABEL))
    expect(TYPE_COLOR).toHaveProperty('punctuation')
    expect(TYPE_LABEL.logic).toBe('逻辑')
  })

  it('未知类型不抛，落到 undefined 由界面兜底', () => {
    expect(TYPE_COLOR['不存在的类型']).toBeUndefined()
  })
})
