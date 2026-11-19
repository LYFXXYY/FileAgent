<script setup lang="ts">
/**
 * 品牌标记 —— 一页纸 + 一道暖橙荧光笔
 *
 * 为什么抽成组件：这个图形在导航胶囊和登录卡片两处出现，原先是两处各写一份 SVG，
 * 改一次就得同步两次，很容易走形。
 *
 * 图形语言跟全站一致：荧光笔划是这套设计的角色动作（导航激活态、表格行悬停、
 * hero 大字都用它），所以品牌标记就是"纸上划过一道"，而不是又一个通用文件图标。
 *
 * 颜色只依赖两个全局令牌，浅底深底都能用：
 * - currentColor 走所在容器的文字色（导航是墨色，登录卡片是白字）
 * - --fa-accent-pop 是中明度暖橙，在墨底和白底上都有足够对比
 */
const props = withDefaults(defineProps<{ size?: number }>(), { size: 20 })
</script>

<template>
  <span class="brand-mark" :style="{ width: props.size + 'px', height: props.size + 'px' }">
    <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <!-- 纸面 + 折角 -->
      <path
        d="M14.6 2.6H7.2A2.2 2.2 0 0 0 5 4.8v14.4a2.2 2.2 0 0 0 2.2 2.2h9.6a2.2 2.2 0 0 0 2.2-2.2V7.4l-4.8-4.8Z"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linejoin="round"
      />
      <path d="M14.6 2.6v4.8h4.6" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" />
      <!-- 未批改的两行 -->
      <path d="M8.4 9.2h4.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" opacity="0.45" />
      <path d="M8.4 16.4h5.4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" opacity="0.45" />
      <!-- 荧光笔划过的那一行：品牌记忆点 -->
      <rect
        x="7.6"
        y="11.9"
        width="8.8"
        height="3.1"
        rx="1.5"
        fill="var(--fa-accent-pop)"
        transform="rotate(-2.2 12 13.45)"
      />
    </svg>
  </span>
</template>

<style scoped>
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
</style>
