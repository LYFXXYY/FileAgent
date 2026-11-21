<script setup lang="ts" generic="T extends object">
/**
 * 手机端卡片列表 —— 五个列表页共用的"行转卡片"外壳
 *
 * 只负责卡片壳（表面/描边/阴影/间距）与空态、loading；
 * 每张卡片内部结构由页面通过默认作用域插槽渲染（各页字段差异大，
 * 抽成配置化渲染器反而难读）。桌面端仍用 el-table，本组件只在
 * useIsMobile 为真时挂载。
 */
defineProps<{
  items: T[]
  loading?: boolean
  keyField?: keyof T & string
}>()
</script>

<template>
  <div v-loading="loading" class="mcard-list">
    <slot v-if="!items.length" name="empty" />
    <div v-for="(row, i) of items" :key="String(keyField ? row[keyField] : i)" class="mcard">
      <slot :row="row" />
    </div>
  </div>
</template>

<style scoped>
.mcard-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  min-height: 120px;
}

.mcard {
  padding: 14px;
  background: var(--fa-surface);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 7%, transparent);
  border-radius: var(--fa-radius-card);
  box-shadow: var(--fa-shadow-lg);
}

/* 卡片里的操作区统一靠右排一行 */
:deep(.mrow-actions) {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-sm);
  margin-top: 10px;
}
</style>
