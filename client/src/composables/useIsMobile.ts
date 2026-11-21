import { onBeforeUnmount, onMounted, ref } from 'vue'

const QUERY = '(max-width: 768px)'

/**
 * 手机端布局开关：与 style.css 的 768px 断点保持同一个阈值。
 * 用 matchMedia 而不是 resize 监听——省掉节流与首帧抖动。
 */
export function useIsMobile() {
  const isMobile = ref(false)
  let mql: MediaQueryList | null = null

  const onChange = (e: MediaQueryListEvent) => {
    isMobile.value = e.matches
  }

  onMounted(() => {
    mql = window.matchMedia(QUERY)
    isMobile.value = mql.matches
    mql.addEventListener('change', onChange)
  })

  onBeforeUnmount(() => {
    mql?.removeEventListener('change', onChange)
  })

  return { isMobile }
}
