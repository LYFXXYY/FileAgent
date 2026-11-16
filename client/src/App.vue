<script setup lang="ts">
/**
 * 根组件 —— 只做两件事：套中文语言包、渲染 <RouterView/>
 *
 * 【概念】
 * - RouterView 会根据当前 URL 渲染匹配到的路由组件
 * - 例如访问 /login → 渲染 Login.vue；访问 / 或 /tasks → 渲染 AppLayout.vue（其内含子 RouterView）
 * - 布局嵌套由路由配置里的 children 决定
 * - locale 原来靠 app.use(ElementPlus, { locale }) 全局注入；改成按需引入后
 *   换成 el-config-provider 包在根上，效果一样（分页、日期选择等内置文案走中文）
 */
import zhCn from 'element-plus/es/locale/lang/zh-cn'
</script>

<template>
  <el-config-provider :locale="zhCn">
    <RouterView />
  </el-config-provider>
</template>
