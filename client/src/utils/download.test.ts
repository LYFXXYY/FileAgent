/**
 * 下载文件名解析测试
 *
 * 钉住三件事：
 * 1. 交付名以服务端为准——前端绝不能按 original_filename 推扩展名
 *    （历史上 Logs 页就是这么写的，PDF 输入批改出的 docx 被命名成 .pdf）；
 * 2. 中文文件名走 RFC 5987 百分号编码，必须 decode 回原样；
 * 3. 头缺失或编码畸形时退回兜底名，而不是抛异常让下载整体失败。
 */
import { describe, expect, it } from 'vitest'

import { resolveFilename } from './download'

describe('resolveFilename', () => {
  it('还原 UTF-8 编码的中文交付名', () => {
    const disp = `attachment; filename*=UTF-8''%E6%89%B9%E6%94%B9_%E6%8A%A4%E7%90%86.docx`
    expect(resolveFilename(disp, 'fallback.docx')).toBe('批改_护理.docx')
  })

  it('md 与 pptx 的交付名各按服务端给的扩展名', () => {
    expect(resolveFilename("attachment; filename*=UTF-8''%E8%BD%AC%E6%8D%A2_a.md", 'fb')).toBe(
      '转换_a.md',
    )
    expect(resolveFilename("attachment; filename*=UTF-8''%E8%BD%AC%E6%8D%A2_a.pptx", 'fb')).toBe(
      '转换_a.pptx',
    )
  })

  it('没有 Content-Disposition 时用兜底名', () => {
    expect(resolveFilename(undefined, 'result_a.pdf')).toBe('result_a.pdf')
    expect(resolveFilename(null, 'result_a.pdf')).toBe('result_a.pdf')
    expect(resolveFilename('', 'result_a.pdf')).toBe('result_a.pdf')
  })

  it('畸形百分号编码退回兜底名，不抛', () => {
    expect(resolveFilename("attachment; filename*=UTF-8''%E4%B8%AD%", 'result_a.docx')).toBe(
      'result_a.docx',
    )
  })

  it('只认 UTF-8 形式，不带该形式时用兜底名', () => {
    expect(resolveFilename('attachment; filename="a.docx"', 'result_a.docx')).toBe(
      'result_a.docx',
    )
  })
})
