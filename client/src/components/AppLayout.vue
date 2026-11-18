<script setup lang="ts">
/**
 * 应用主布局 —— 悬浮胶囊导航 + 主内容区
 *
 * 【Vue 概念速览】
 * - <script setup> 是 Vue 3 的编译时糖，把 ref/computed 直接暴露到模板
 *   不用 return，也不用 export default { setup() {} }
 * - <RouterView/> 是 vue-router 的占位组件，渲染当前匹配的子路由
 *   类比 Spring 的 <tiles:insertDefinition/> 或者 Thymeleaf 的 layout:fragment
 *
 * 【导航设计】
 * - 悬浮胶囊（fixed + rounded-full + backdrop blur）手法来自 UI-REFERENCE §3.4/§10.2，
 *   数值为参考站 computed style 实测：高 48px、blur(24px)、30% 透明白、hover 才加深阴影
 * - el-menu 换成 horizontal 模式，激活态自绘成墨底白字小胶囊，不用组件库默认下划线
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// 挂载即刷新身份快照：role / can_use_default 是登录时冻结进 localStorage 的，
// 管理员在 Admin 页改了授权，被改动者下次进入应用就能看到正确视图（无需重登）。
// 内部已吞错，不阻塞启动。
onMounted(() => {
  auth.refreshProfile()
})

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
  <div class="app-shell">
    <!-- ============ 悬浮胶囊导航 ============ -->
    <header class="float-nav">
      <div class="brand">
        <!-- 品牌图标：文件 + AI 概念，与 Login.vue 保持一致 -->
        <span class="brand-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M14 2H6C4.9 2 4 2.9 4 4V20C4 21.1 4.9 22 6 22H18C19.1 22 20 21.1 20 20V8L14 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M14 2V8H20" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M8 13H16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <path d="M8 17H13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </span>
        <span class="brand-name">FileAgent</span>
      </div>

      <el-menu :default-active="activeMenu" router mode="horizontal" :ellipsis="false" class="nav-menu">
        <!-- router 属性让 el-menu-item 变成 <router-link>，index 就是要跳转的路径 -->
        <el-menu-item index="/">
          <el-icon>
            <!-- 工作台：房子图标 -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M3 9L12 2L21 9V20C21 21.1 20.1 22 19 22H5C3.9 22 3 21.1 3 20V9Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
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
              <path d="M20 12V19C20 20.1 19.1 21 18 21H5C3.9 21 3 20.1 3 19V5C3 3.9 3.9 3 5 3H16" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/providers">
          <el-icon>
            <!-- 模型提供商：机器人图标（对外接 LLM 服务，机器人比钥匙更贴语义） -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="12" cy="2.6" r="1.4" fill="currentColor" stroke="none"/>
              <path d="M12 4.4V6.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <rect x="4" y="6.5" width="16" height="13" rx="4" stroke="currentColor" stroke-width="2"/>
              <circle cx="9" cy="12" r="1.2" fill="currentColor" stroke="none"/>
              <circle cx="15" cy="12" r="1.2" fill="currentColor" stroke="none"/>
              <path d="M9.5 16H14.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <path d="M1.5 11V14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <path d="M22.5 11V14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
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
        <!-- 管理入口：仅 admin 可见（路由守卫 + 后端 require_admin 双重兜底） -->
        <el-menu-item v-if="auth.isAdmin" index="/admin">
          <el-icon>
            <!-- 用户管理：人群图标 -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M17 21V19C17 16.79 14.76 15 12 15C9.24 15 7 16.79 7 19V21" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <circle cx="12" cy="7" r="4" stroke="currentColor" stroke-width="2"/>
              <path d="M20 21V19C20 17.5 19 16.2 17.5 15.4M4 21V19C4 17.5 5 16.2 6.5 15.4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </el-icon>
          <span>用户管理</span>
        </el-menu-item>
      </el-menu>

      <div class="header-right">
        <span class="user-dot" aria-hidden="true"></span>
        <span class="username">{{ auth.username }}</span>
        <el-button link type="primary" @click="handleLogout">退出</el-button>
      </div>
    </header>

    <!-- ============ 主区域 ============ -->
    <main class="app-main">
      <!-- 子路由内容渲染在这里 -->
      <!-- keep-alive 让工作台在处理过程中切换页面时不被销毁，状态自然保留 -->
      <RouterView v-slot="{ Component }">
        <keep-alive>
          <component :is="Component" />
        </keep-alive>
      </RouterView>
    </main>
  </div>
</template>

<style scoped>
/* scoped 属性：这些 CSS 只作用于本组件，不会污染全局 */

.app-shell {
  min-height: 100vh;
}

/* ============ 悬浮胶囊导航 ============ */
.float-nav {
  position: fixed;
  top: 16px;
  left: 16px;
  right: 16px;
  z-index: 100;
  height: 48px;
  display: flex;
  align-items: center;
  gap: var(--space-md);
  padding: 0 6px 0 18px;
  border-radius: var(--fa-radius-pill);
  background: color-mix(in oklab, var(--fa-surface) 70%, transparent);
  -webkit-backdrop-filter: blur(24px);
  backdrop-filter: blur(24px);
  box-shadow: var(--fa-shadow-lg);
  transition:
    background var(--fa-duration-fast) ease-out,
    box-shadow var(--fa-duration-fast) ease-out;
}

.float-nav:hover {
  background: color-mix(in oklab, var(--fa-surface) 84%, transparent);
  box-shadow: var(--fa-shadow-xl);
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  white-space: nowrap;
}

.brand-icon {
  color: var(--fa-ink);
  display: flex;
  align-items: center;
}

/* ============ 横向菜单：去组件库默认外观，激活态自绘 ============ */
.nav-menu {
  --el-menu-bg-color: transparent;
  --el-menu-text-color: var(--el-text-color-regular);
  --el-menu-active-color: var(--fa-ink);
  --el-menu-hover-bg-color: transparent;
  border-bottom: none !important;
  background: transparent;
  flex: 1;
  min-width: 0;
}

.nav-menu :deep(.el-menu-item) {
  height: 36px;
  margin: 0 2px;
  padding: 0 14px;
  border-radius: var(--fa-radius-pill);
  font-size: 14px;
  transition: all var(--fa-duration-fast) var(--fa-ease-spring);
}

/* 激活态 = 暖橙荧光笔划过一下（项目角色色，比墨底白字更有性格） */
.nav-menu :deep(.el-menu-item.is-active) {
  background: color-mix(in oklab, var(--fa-accent) 32%, var(--fa-bg));
  --el-menu-active-color: var(--fa-ink);
  color: var(--fa-ink);
  font-weight: 600;
}

.nav-menu :deep(.el-menu-item.is-active .el-icon) {
  color: var(--fa-ink);
}

.nav-menu :deep(.el-menu-item:hover:not(.is-active)) {
  background: color-mix(in oklab, var(--fa-ink) 6%, transparent);
}

/* 窄视口：菜单项只留图标并收紧间距，用户名文字隐退，保证退出不被挤出胶囊 */
@media (max-width: 900px) {
  .nav-menu :deep(.el-menu-item span) {
    display: none;
  }

  .nav-menu :deep(.el-menu-item) {
    padding: 0 8px;
  }

  .username {
    display: none;
  }

  .float-nav {
    gap: var(--space-sm);
    padding-left: 12px;
  }
}

.header-right {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-left: auto;
  padding-right: 8px;
  white-space: nowrap;
}

.username {
  color: var(--el-text-color-regular);
  font-size: 14px;
}

/* 在线状态点：暖橙呼吸，胶囊导航上也有一点“活”的信号 */
.user-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fa-accent-pop);
  animation: dotBreath 1.8s ease-in-out infinite;
}

@keyframes dotBreath {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}

/* ============ 主区域 ============ */
.app-main {
  padding: 88px 24px 32px;
  max-width: calc(var(--app-content-max-width) + 48px);
  margin: 0 auto;
  width: 100%;
}
</style>
