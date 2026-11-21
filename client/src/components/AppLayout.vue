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
 *
 * 【手机端（≤768px）】
 * - 顶栏瘦成"品牌 + 用户名"细条，导航整体下移到固定底部 TabBar（工作台/任务/日志/模型/更多）；
 *   规范库、用户管理、退出收进"更多"弹层——6 个入口在 375px 放不下，砍入口不如分组
 * - 断点阈值与 useIsMobile / style.css 三处同源（768px），改一处要想起另两处
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import { markReady } from '@/router/notice'
import { useIsMobile } from '@/composables/useIsMobile'
import BrandMark from '@/components/BrandMark.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { isMobile } = useIsMobile()
const moreOpen = ref(false)

// 挂载即刷新身份快照：role / can_use_default 是登录时冻结进 localStorage 的，
// 管理员在 Admin 页改了授权，被改动者下次进入应用就能看到正确视图（无需重登）。
// 内部已吞错，不阻塞启动。
onMounted(() => {
  auth.refreshProfile()
  // 布局挂载 = app 就绪，此后守卫提示可以正常投递；顺带补投冷加载时攒下的一条
  markReady()
})

// 当前激活的菜单项 —— 用路径反推，保证刷新后仍能高亮
const activeMenu = computed(() => route.path)

// "更多"里装着规范库/用户管理——这些路由激活时"更多"按钮也要亮
const isMoreRoute = computed(() => activeMenu.value === '/specs' || activeMenu.value === '/admin')

async function handleLogout() {
  moreOpen.value = false
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
    <!-- ============ 悬浮胶囊导航（桌面） ============ -->
    <header v-if="!isMobile" class="float-nav">
      <div class="brand">
        <!-- 品牌标记与登录卡片共用同一个组件，不再两处各画一份 -->
        <BrandMark :size="28" class="brand-icon" />
        <span class="brand-name"><b>File</b><i>Agent</i></span>
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
        <el-menu-item index="/specs">
          <el-icon>
            <!-- 规范库：一本立着的书（与 Specs.vue 页头同一图形） -->
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H19v18H5.5A1.5 1.5 0 0 1 4 19.5V4.5Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
              <path d="M8 3v18" stroke="currentColor" stroke-width="2"/>
            </svg>
          </el-icon>
          <span>规范库</span>
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

    <!-- ============ 手机端：细顶栏 + 底部 TabBar ============ -->
    <header v-else class="mobile-top">
      <div class="brand">
        <BrandMark :size="24" class="brand-icon" />
        <span class="brand-name"><b>File</b><i>Agent</i></span>
      </div>
      <span class="username">{{ auth.username }}</span>
    </header>

    <nav v-if="isMobile" class="tabbar">
      <router-link class="tab" to="/" :class="{ 'is-active': activeMenu === '/' }">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
          <path d="M3 9L12 2L21 9V20C21 21.1 20.1 22 19 22H5C3.9 22 3 21.1 3 20V9Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M9 22V12H15V22" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>工作台</span>
      </router-link>
      <router-link class="tab" to="/tasks" :class="{ 'is-active': activeMenu === '/tasks' }">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
          <path d="M9 11L12 14L20 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M20 12V19C20 20.1 19.1 21 18 21H5C3.9 21 3 20.1 3 19V5C3 3.9 3.9 3 5 3H16" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>任务</span>
      </router-link>
      <router-link class="tab" to="/logs" :class="{ 'is-active': activeMenu === '/logs' }">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>
          <path d="M12 6V12L16 14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span>日志</span>
      </router-link>
      <router-link class="tab" to="/providers" :class="{ 'is-active': activeMenu === '/providers' }">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
          <rect x="4" y="6.5" width="16" height="13" rx="4" stroke="currentColor" stroke-width="2"/>
          <circle cx="9" cy="12" r="1.2" fill="currentColor" stroke="none"/>
          <circle cx="15" cy="12" r="1.2" fill="currentColor" stroke="none"/>
          <path d="M9.5 16H14.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
        <span>模型</span>
      </router-link>
      <el-popover v-model:visible="moreOpen" placement="top-end" :width="180" trigger="click">
        <template #reference>
          <button class="tab" :class="{ 'is-active': isMoreRoute }" type="button">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <circle cx="5" cy="12" r="1.8" fill="currentColor" stroke="none"/>
              <circle cx="12" cy="12" r="1.8" fill="currentColor" stroke="none"/>
              <circle cx="19" cy="12" r="1.8" fill="currentColor" stroke="none"/>
            </svg>
            <span>更多</span>
          </button>
        </template>
        <div class="more-sheet">
          <router-link class="more-item" to="/specs" @click="moreOpen = false">规范库</router-link>
          <router-link v-if="auth.isAdmin" class="more-item" to="/admin" @click="moreOpen = false">用户管理</router-link>
          <button class="more-item more-danger" type="button" @click="handleLogout">退出登录</button>
        </div>
      </el-popover>
    </nav>

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
  /* 64px：原先 48 的横条把品牌位一起压小了，字标和 logo 都撑不起来 */
  height: 64px;
  display: flex;
  align-items: center;
  gap: var(--space-lg);
  padding: 0 10px 0 26px;
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
  gap: 10px;
  white-space: nowrap;
}

.brand-icon {
  color: var(--fa-ink);
  display: flex;
  align-items: center;
}

/* 字标：File 墨黑、Agent 暖橙，底下压一道荧光笔 —— 与导航激活态同一个动作 */
.brand-name {
  font-size: var(--fa-text-xl);
  letter-spacing: -0.025em;
  line-height: 1;
}

.brand-name b {
  font-weight: 800;
  color: var(--fa-ink);
}

.brand-name i {
  position: relative;
  z-index: 0;
  font-style: normal;
  font-weight: 700;
  padding: 0 2px;
  color: var(--fa-accent);
}

.brand-name i::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 5px;
  z-index: -1;
  border-radius: 2px;
  background: color-mix(in oklab, var(--fa-accent-pop) 38%, transparent);
  transform: rotate(-1.4deg);
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
  /* el-menu 自身高度是 60（组件库默认值），菜单项只有 40。
     横向菜单的交叉轴默认是 stretch，但显式给了 height 后子项会贴顶，
     必须在这里居中，否则导航文字在胶囊里偏上（2026-11-19 截图实拍） */
  display: flex;
  align-items: center;
}

.nav-menu :deep(.el-menu-item) {
  display: inline-flex;
  align-items: center;
  height: 40px;
  margin: 0 2px;
  padding: 0 18px;
  border-radius: var(--fa-radius-pill);
  font-size: var(--fa-text-base);
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
  font-size: var(--fa-text-base);
}

/* 在线状态点：暖橙呼吸，胶囊导航上也有一点“活”的信号（dotBreath 在全局 style.css） */
.user-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fa-accent-pop);
  animation: dotBreath var(--fa-duration-pulse) ease-in-out infinite;
}

/* ============ 主区域 ============ */
.app-main {
  /* 104px = 胶囊 top 16 + 高 64 + 24 呼吸位 */
  padding: 104px 24px 32px;
  max-width: calc(var(--app-content-max-width) + 48px);
  margin: 0 auto;
  width: 100%;
}

/* ============ 手机端（≤768px，与 useIsMobile 同阈值） ============ */
.mobile-top {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  background: color-mix(in oklab, var(--fa-surface) 82%, transparent);
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
  box-shadow: var(--fa-shadow-md);
}

.mobile-top .username {
  font-size: var(--fa-text-sm);
}

.tabbar {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 100;
  display: flex;
  align-items: stretch;
  height: calc(58px + env(safe-area-inset-bottom));
  padding-bottom: env(safe-area-inset-bottom);
  background: color-mix(in oklab, var(--fa-surface) 90%, transparent);
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
  box-shadow: 0 -2px 14px color-mix(in oklab, var(--fa-ink) 10%, transparent);
}

.tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  border: none;
  background: transparent;
  color: var(--el-text-color-regular);
  font-size: var(--fa-text-xs);
  text-decoration: none;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

/* 激活态与桌面导航同一个动作：暖橙荧光笔垫底 */
.tab.is-active {
  color: var(--fa-ink);
  font-weight: 600;
  background: color-mix(in oklab, var(--fa-accent) 30%, var(--fa-bg));
  border-radius: 14px 14px 0 0;
}

.more-sheet {
  display: flex;
  flex-direction: column;
}

.more-item {
  padding: 10px 14px;
  border: none;
  background: transparent;
  border-radius: 8px;
  color: var(--el-text-color-primary);
  font-size: var(--fa-text-base);
  text-align: left;
  text-decoration: none;
  cursor: pointer;
}

.more-item:hover {
  background: color-mix(in oklab, var(--fa-ink) 6%, transparent);
}

.more-danger {
  color: var(--el-color-danger);
}

@media (max-width: 768px) {
  .app-main {
    /* 顶栏 52 + 呼吸 12；底部给 TabBar 让出 58 + 安全区 + 12 */
    padding: 64px 12px calc(78px + env(safe-area-inset-bottom));
  }
}
</style>
