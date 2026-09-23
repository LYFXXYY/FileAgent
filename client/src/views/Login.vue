<script setup lang="ts">
/**
 * 登录/注册页
 *
 * 【Vue 概念】
 * - ref() 声明响应式数据，模板用 xxx.value 或直接 xxx（Vue 会自动 unwrap）
 * - v-model="form.username" 双向绑定，输入框改动自动同步到 form 对象
 * - @submit.prevent 阻止表单原生提交（走 JS handler）
 * - el-form :rules 定义校验规则，validate() 手动触发
 */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

// 当前 tab: login / register
const tab = ref<'login' | 'register'>('login')
const loading = ref(false)
const formRef = ref<FormInstance>()

const form = reactive({
  username: '',
  password: '',
})

// 表单校验规则（前端第一层校验，后端还有第二层）
const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '用户名长度 3-50', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 72, message: '密码长度 6-72', trigger: 'blur' },
  ],
}

async function handleSubmit() {
  // validate() 返回 boolean，字段不合法时会自动给 form-item 加红字错误
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    if (tab.value === 'login') {
      await auth.login(form.username, form.password)
      ElMessage.success('登录成功')
    } else {
      await auth.register(form.username, form.password)
      ElMessage.success('注册成功，已自动登录')
    }
    // 支持 ?redirect=/tasks 参数，登录后回到原目标页
    const redirect = (route.query.redirect as string) || '/'
    router.replace(redirect)
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    loading.value = false
  }
}

function switchTab(name: string | number) {
  tab.value = name as 'login' | 'register'
  // 切换后清一次校验状态，避免红字错误残留
  formRef.value?.clearValidate()
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <!-- Logo 图标：文件 + AI 概念 -->
        <div class="logo-icon">
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M14 2H6C5.46957 2 4.96086 2.21071 4.58579 2.58579C4.21071 2.96086 4 3.46957 4 4V20C4 20.5304 4.21071 21.0391 4.58579 21.4142C4.96086 21.7893 5.46957 22 6 22H18C18.5304 22 19.0391 21.7893 19.4142 21.4142C19.7893 21.0391 20 20.5304 20 20V8L14 2Z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M14 2V8H20" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M8 13H16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <path d="M8 17H13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </div>
        <h1 class="login-title">FileAgent</h1>
        <p class="login-subtitle">Word 文档智能处理平台</p>
      </div>

      <el-tabs v-model="tab" stretch @tab-change="switchTab">
        <el-tab-pane label="登录" name="login" />
        <el-tab-pane label="注册" name="register" />
      </el-tabs>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="handleSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <div class="input-with-icon">
            <span class="input-icon">
              <!-- 用户图标 -->
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M20 21V19C20 17.9391 19.5786 16.9217 18.8284 16.1716C18.0783 15.4214 17.0609 15 16 15H8C6.93913 15 5.92172 15.4214 5.17157 16.1716C4.42143 16.9217 4 17.9391 4 19V21" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <circle cx="12" cy="7" r="4" stroke="currentColor" stroke-width="2"/>
              </svg>
            </span>
            <el-input
              v-model="form.username"
              placeholder="3-50 位，字母/数字/下划线/中文"
              autocomplete="username"
            />
          </div>
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <div class="input-with-icon">
            <span class="input-icon">
              <!-- 锁图标 -->
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2" stroke="currentColor" stroke-width="2"/>
                <path d="M7 11V7C7 5.67392 7.52678 4.40215 8.46447 3.46447C9.40215 2.52678 10.6739 2 12 2C13.3261 2 14.5979 2.52678 15.5355 3.46447C16.4732 4.40215 17 5.67392 17 7V11" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </span>
            <el-input
              v-model="form.password"
              type="password"
              placeholder="6-72 位"
              autocomplete="current-password"
              show-password
              @keyup.enter="handleSubmit"
            />
          </div>
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            class="submit-btn"
            @click="handleSubmit"
          >
            {{ tab === 'login' ? '登录' : '注册并登录' }}
          </el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
/* 全屏容器：渐变背景 + 微妙的几何纹理 */
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #e8f0fe 0%, #f5f7fa 50%, #eef2ff 100%);
  position: relative;
  overflow: hidden;
}

/* 装饰性背景圆：增加视觉层次感 */
.login-page::before,
.login-page::after {
  content: '';
  position: absolute;
  border-radius: 50%;
  opacity: 0.15;
  pointer-events: none;
}

.login-page::before {
  width: 400px;
  height: 400px;
  background: radial-gradient(circle, #409eff 0%, transparent 70%);
  top: -100px;
  right: -100px;
}

.login-page::after {
  width: 300px;
  height: 300px;
  background: radial-gradient(circle, #67c23a 0%, transparent 70%);
  bottom: -80px;
  left: -80px;
}

/* 登录卡片：玻璃态效果 + 悬停动效 */
.login-card {
  width: 400px;
  padding: var(--space-xl);
  background: rgba(255, 255, 255, 0.95);
  backdrop-filter: blur(10px);
  border-radius: 16px;
  border: 1px solid rgba(255, 255, 255, 0.8);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.08);
  transition: all 0.3s ease;
  position: relative;
  z-index: 1;
  animation: fadeInUp 0.5s ease-out;
}

.login-card:hover {
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
  transform: translateY(-4px);
}

/* 头部区域：Logo + 标题 */
.login-header {
  text-align: center;
  margin-bottom: var(--space-xl);
}

/* Logo 图标容器 */
.logo-icon {
  width: 64px;
  height: 64px;
  margin: 0 auto var(--space-md);
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 16px;
  background: linear-gradient(135deg, #409eff 0%, #66b1ff 100%);
  color: #ffffff;
  box-shadow: 0 4px 12px rgba(64, 158, 255, 0.3);
}

.login-title {
  font-size: 28px;
  font-weight: 700;
  background: linear-gradient(135deg, #303133 0%, #409eff 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin: 0;
  letter-spacing: -0.5px;
}

.login-subtitle {
  font-size: 14px;
  color: #909399;
  margin: 8px 0 0;
}

/* Tab 切换样式优化 */
:deep(.el-tabs__nav-wrap::after) {
  height: 1px;
  background: linear-gradient(90deg, transparent, #e4e7ed, transparent);
}

:deep(.el-tabs__item) {
  font-weight: 500;
  transition: all 0.3s ease;
}

:deep(.el-tabs__item.is-active) {
  color: #409eff;
}

:deep(.el-tabs__active-bar) {
  background: linear-gradient(90deg, #409eff, #66b1ff);
  height: 3px;
  border-radius: 2px;
}

/* 输入框前缀图标 */
.input-with-icon {
  position: relative;
  width: 100%;
}

.input-icon {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: #909399;
  z-index: 1;
  pointer-events: none;
  display: flex;
  align-items: center;
}

:deep(.el-input__wrapper) {
  padding-left: 36px;
  transition: all 0.3s ease;
}

:deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px #c0c4cc inset;
}

:deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px #409eff inset !important;
}

/* 提交按钮：渐变背景 + 悬停动效 */
.submit-btn {
  width: 100%;
  background: linear-gradient(135deg, #409eff 0%, #66b1ff 100%);
  border: none;
  font-weight: 600;
  letter-spacing: 0.5px;
  transition: all 0.3s ease;
}

.submit-btn:hover {
  background: linear-gradient(135deg, #66b1ff 0%, #409eff 100%);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(64, 158, 255, 0.35);
}

.submit-btn:active {
  transform: translateY(0);
}

/* 淡入动画 */
/* fadeInUp 已提取到全局 style.css，此处保留占位注释以说明来源 */

/* 响应式 */
@media (max-width: 480px) {
  .login-card {
    width: 90%;
    max-width: 360px;
    padding: var(--space-lg);
  }
  
  .login-title {
    font-size: 24px;
  }
}
</style>
