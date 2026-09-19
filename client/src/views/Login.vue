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
    <el-card class="login-card" shadow="never">
      <div class="login-header">
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
          <el-input
            v-model="form.username"
            placeholder="3-50 位，字母/数字/下划线/中文"
            autocomplete="username"
          />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="6-72 位"
            autocomplete="current-password"
            show-password
            @keyup.enter="handleSubmit"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            style="width: 100%"
            @click="handleSubmit"
          >
            {{ tab === 'login' ? '登录' : '注册并登录' }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f7fa;
}

.login-card {
  width: 380px;
}

.login-header {
  text-align: center;
  margin-bottom: var(--space-lg);
}

.login-title {
  font-size: 24px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.login-subtitle {
  font-size: 13px;
  color: #909399;
  margin: 6px 0 0;
}
</style>
