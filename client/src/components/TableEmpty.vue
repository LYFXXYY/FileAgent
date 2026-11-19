<script setup lang="ts">
/**
 * 列表页空态 —— 紧凑、有出口
 *
 * 为什么不用 el-empty：它的插图默认占 120px，加上内边距实测把空表撑到 439px 高，
 * 页面看起来一半是空的（2026-11-19 实测）。这里压到 ~150px，并且强制给一个动作，
 * 让空白变成引导而不是留白。
 */
withDefaults(
  defineProps<{
    title: string
    hint?: string
    actionText?: string
  }>(),
  { hint: '', actionText: '' },
)

const emit = defineEmits<{ action: [] }>()
</script>

<template>
  <div class="table-empty">
    <span class="empty-mark" aria-hidden="true">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
        <path
          d="M14.6 2.6H7.2A2.2 2.2 0 0 0 5 4.8v14.4a2.2 2.2 0 0 0 2.2 2.2h9.6a2.2 2.2 0 0 0 2.2-2.2V7.4l-4.8-4.8Z"
          stroke="currentColor"
          stroke-width="1.7"
          stroke-linejoin="round"
          opacity="0.5"
        />
        <rect
          x="7.6"
          y="11.4"
          width="8.8"
          height="3.1"
          rx="1.5"
          fill="var(--fa-accent-pop)"
          transform="rotate(-2.2 12 12.95)"
        />
      </svg>
    </span>
    <p class="empty-title">{{ title }}</p>
    <p v-if="hint" class="empty-hint">{{ hint }}</p>
    <el-button v-if="actionText" type="primary" @click="emit('action')">
      {{ actionText }}
    </el-button>
  </div>
</template>

<style scoped>
.table-empty {
  display: flex;
  flex-direction: column;
  /* 左对齐整块：原先居中 + 42ch 上限，长句被切成两行居中排（2026-11-19 截图实拍） */
  align-items: flex-start;
  gap: 10px;
  padding: var(--space-lg) var(--space-md);
}

.empty-mark {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 52px;
  height: 52px;
  border-radius: 16px;
  color: var(--fa-ink);
  background: color-mix(in oklab, var(--fa-ink) 5%, var(--fa-bg));
  border: 1px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
}

.empty-title {
  margin: 4px 0 0;
  font-size: var(--fa-text-md);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.empty-hint {
  margin: 0;
  font-size: var(--fa-text-sm);
  line-height: 1.7;
  color: var(--el-text-color-secondary);
  text-align: left;
}
</style>
