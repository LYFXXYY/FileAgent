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

import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// 当前激活的菜单项 —— 用路径反推，保证刷新后仍能高亮
const activeMenu = computed(() => route.path)

async function handleLogout() {
  // Element Plus 的确认框，返回 Promise，用户取消会 reject
  try {
    await ElMessageBox.confirm('确定退出登录吗？', '提示', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return // 用户点了取消，直接退出函数
  }
  auth.logout()
  router.replace('/login')
}
</script>

<template>
  <el-container class="app-shell">
    <!-- ============ 侧栏 ============ -->
    <el-aside :width="'var(--app-sidebar-width)'" class="app-sidebar">
      <div class="brand">
        <!-- 品牌图标：文件 + AI 概念，与 Login.vue 保持一致 -->
        <span class="brand-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M14 2H6C5.46957 2 4.96086 2.21071 4.58579 2.58579C4.21071 2.96086 4 3.46957 4 4V20C4 20.5304 4.21071 21.0391 4.58579 21.4142C4.96086 21.7893 5.46957 22 6 22H18C18.5304 22 19.0391 21.7893 19.4142 21.4142C19.7893 21.0391 20 20.5304 20 20V8L14 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M14 2V8H20" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M8 13H16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <path d="M8 17H13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </span>
        <span>FileAgent</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        class="sidebar-menu"
      >
        <!-- router 属性让 el-menu-item 变成 <router-link>，index 就是要跳转的路径 -->
        <el-menu-item index="/">
          <el-icon>
            <!-- 工作台：房子图标 -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M3 9L12 2L21 9V20C21 20.5304 20.7893 21.0391 20.4142 21.4142C20.0391 21.7893 19.5304 22 19 22H5C4.46957 22 3.96086 21.7893 3.58579 21.4142C3.21071 21.0391 3 20.5304 3 20V9Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M9 22V12H15V22" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </el-icon>
          <span>工作台</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon>
            <!-- 任务管理：清单图标 -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M9 11L12 14L20 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M20 12V19C20 19.5304 19.7893 20.0391 19.4142 20.4142C19.0391 20.7893 18.5304 21 18 21H5C4.46957 21 3.96086 20.7893 3.58579 20.4142C3.21071 20.0391 3 19.5304 3 19V5C3 4.46957 3.21071 3.96086 3.58579 3.58579C3.96086 3.21071 4.46957 3 5 3H16" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/providers">
          <el-icon>
            <!-- 模型提供商：钥匙图标 -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M21 2L19 4M11.3891 13.3912C9.82881 14.9515 7.30015 14.9515 5.73985 13.3912C4.17956 11.8309 4.17956 9.30224 5.73985 7.74195C7.30015 6.18165 9.82881 6.18165 11.3891 7.74195L19 2M19 2L22 5L18 9L15 6L19 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </el-icon>
          <span>模型提供商</span>
        </el-menu-item>
        <el-menu-item index="/logs">
          <el-icon>
            <!-- 操作日志：时钟图标 -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>
              <path d="M12 6V12L16 14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </el-icon>
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
        <!-- keep-alive 让工作台在处理过程中切换页面时不被销毁，状态自然保留 -->
        <RouterView v-slot="{ Component }">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </RouterView>
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
  display: flex;
  align-items: center;
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
