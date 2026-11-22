/**
 * 结果文件下载 —— 从服务端响应还原文件名并触发浏览器下载
 *
 * 【为什么文件名必须以服务端为准】
 * 交付名由 task.output_format 决定（批改_x.docx / 转换_x.pptx / 转换_x.md 或 .zip），
 * 与输入的 original_filename 没有推导关系。前端自己拼 `result_${original_filename}`
 * 会把一份 PDF 输入批改出的 docx 命名成 .pdf，md 任务连扩展名都对不上。
 * 后端下载路由统一走 RFC 5987：filename*=UTF-8''<percent-encoded>。
 */

const UTF8_FILENAME = /filename\*=UTF-8''([^;]+)/i

/**
 * 解析 Content-Disposition 里的文件名，拿不到就用兜底名
 *
 * 畸形百分号编码（上游截断/header 被代理改写）不该让整个下载失败，
 * 所以 decodeURIComponent 的异常退回到兜底名而不是往上抛。
 */
export function resolveFilename(
  contentDisposition: string | null | undefined,
  fallback: string,
): string {
  const match = contentDisposition?.match(UTF8_FILENAME)
  if (!match?.[1]) return fallback
  try {
    return decodeURIComponent(match[1])
  } catch {
    return fallback
  }
}

/**
 * 把 blob 存成文件
 *
 * 两点是浏览器差异逼出来的，不是风格偏好：
 * - <a> 必须先挂进文档再 click()，Firefox 对游离元素不触发下载；
 * - 同步 revokeObjectURL 会让部分浏览器还没开始传输就拿到空 blob，
 *   所以延后一拍释放。
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
