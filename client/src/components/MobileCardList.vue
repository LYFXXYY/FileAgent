<script setup lang="ts" generic="T extends { id: number }">
/**
 * 手机端卡片列表 —— 五个列表页共用的"行转卡片"外壳
 *
 * 只负责卡片壳（表面/描边/阴影/间距）与空态、loading；
 * 每张卡片内部结构由页面通过默认作用域插槽渲染（各页字段差异大，
 * 抽成配置化渲染器反而难读）。桌面端仍用 el-table，本组件只在
 * useIsMobile 为真时挂载。
 *
 * 泛型约束到 { id: number } 而不是裸 object：卡片里含下拉与开关的页面
 * （Admin）在 onActivated 重取数组后，用数组下标当 key 会把组件内部状态
 * 绑到错位的那一行。id 是这五个列表项共有的主键，直接当约束比留一个
 * "可传可不传"的 keyField 更难被漏掉。
 */
defineProps<{
  items: T[]
  loading?: boolean
}>()
</script>

<template>
  <div v-loading="loading" class="mcard-list">
    <slot v-if="!items.length" name="empty" />
    <div v-for="row of items" :key="row.id" class="mcard">
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
