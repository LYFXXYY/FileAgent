<script setup lang="ts">
/**
 * 应用主布局 —— 侧栏导航 + 顶栏 + 主内容 slot
 *
 * 【Vue 概念速览】
 * - <script setup> 是 Vue 3 的编译时糖，把 ref/computed 直接暴露到模板
 *   不用 return，也不用 export default { setup() {} }
 * - <RouterView/> 是 vue-router 的占位组件，渲染当前匹配的子路由
 *   类比 Spring 的 <tiles:insertDefinition/> 或者 Thymeleaf 的 layout:fragment
 *
 * 【Element Plus 布局组件】
 * - el-container 是 flex 容器，el-aside/el-main/el-header 自动摆放
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { Document, Folder, Key, Setting } from '@element-plus/icons-vue'

import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// 当前激活的菜单项 —— 用路径反推，保证刷新后仍能高亮
const activeMenu = computed(() => route.path)

async function handleLogout() {
  // Element Plus 的确认框，返回 Promise，用户取消会 reject
  await ElMessageBox.confirm('确定退出登录吗？', '提示', {
    confirmButtonText: '退出',
    cancelButtonText: '取消',
    type: 'warning',
  })
  auth.logout()
  router.replace('/login')
}
</script>

<template>
  <el-container class="app-shell">
    <!-- ============ 侧栏 ============ -->
    <el-aside :width="'var(--app-sidebar-width)'" class="app-sidebar">
      <div class="brand">
        <el-icon class="brand-icon"><Document /></el-icon>
        <span>FileAgent</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        class="sidebar-menu"
      >
        <!-- router 属性让 el-menu-item 变成 <router-link>，index 就是要跳转的路径 -->
        <el-menu-item index="/">
          <el-icon><Folder /></el-icon>
          <span>工作台</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><Setting /></el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/providers">
          <el-icon><Key /></el-icon>
          <span>模型提供商</span>
        </el-menu-item>
        <el-menu-item index="/logs">
          <el-icon><Document /></el-icon>
          <span>操作日志</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <!-- ============ 主区域 ============ -->
    <el-container>
      <el-header class="app-header" height="var(--app-header-height)">
        <div class="header-right">
          <span class="username">{{ auth.username }}</span>
          <el-button link type="primary" @click="handleLogout">退出</el-button>
        </div>
      </el-header>
      <el-main class="app-main">
        <!-- 子路由内容渲染在这里 -->
        <RouterView />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
/* scoped 属性：这些 CSS 只作用于本组件，不会污染全局 */
/* 类比 Java 里没有直接对应，可以理解为组件级私有样式 */

.app-shell {
  height: 100vh;
}

.app-sidebar {
  background: var(--sidebar-bg);
  border-right: 1px solid var(--sidebar-border);
  display: flex;
  flex-direction: column;
}

.brand {
  height: var(--app-header-height);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 20px;
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  border-bottom: 1px solid var(--sidebar-border);
}

.brand-icon {
  color: #409eff;
  font-size: 20px;
}

.sidebar-menu {
  border-right: none;
  flex: 1;
}

.app-header {
  background: #ffffff;
  border-bottom: 1px solid var(--sidebar-border);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding: 0 var(--space-lg);
}

.header-right {
  display: flex;
  align-items: center;
  gap: var(--space-md);
}

.username {
  color: #606266;
  font-size: 14px;
}

.app-main {
  background: #f5f7fa;
  padding: var(--space-lg);
  overflow-y: auto;
}
</style>
