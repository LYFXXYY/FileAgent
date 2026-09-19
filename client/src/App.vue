<script setup lang="ts">
/**
 * 根组件 —— 只放一个 <RouterView/>
 *
 * 【概念】
 * - RouterView 会根据当前 URL 渲染匹配到的路由组件
 * - 例如访问 /login → 渲染 Login.vue；访问 / 或 /tasks → 渲染 AppLayout.vue（其内含子 RouterView）
 * - 布局嵌套由路由配置里的 children 决定
 */
</script>

<template>
  <RouterView />
</template>
