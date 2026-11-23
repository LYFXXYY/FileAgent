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
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'
import BrandMark from '@/components/BrandMark.vue'
import {
  PASSWORD_MAX,
  PASSWORD_MIN,
  checkPasswordStrength,
  passwordPlaceholder as registerPasswordPlaceholder,
  weakPasswordHint,
} from '@/utils/passwordPolicy'

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
/**
 * 口令强度只在注册 tab 生效
 *
 * 登录与注册共用一个表单，但**登录侧绝不能套长度规则**：存量用户里有人是 6 位密码，
 * 前端一改下限就会在发请求之前把这些人拦下——等于自己把人锁在门外，而且后端其实
 * 还能验通。真正的强度校验在 `schemas/auth.py`，只管注册路径，这里与它同源。
 * 文案与校验函数都从 `utils/passwordPolicy` 取，改密弹窗用的是同一份。
 */
const rules = computed<FormRules>(() => ({
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '用户名长度 3-50', trigger: 'blur' },
  ],
  password:
    tab.value === 'register'
      ? [
          { required: true, message: '请输入密码', trigger: 'blur' },
          {
            min: PASSWORD_MIN,
            max: PASSWORD_MAX,
            message: weakPasswordHint,
            trigger: 'blur',
          },
          { validator: checkPasswordStrength, trigger: 'blur' },
        ]
      : [{ required: true, message: '请输入密码', trigger: 'blur' }],
}))

/**
 * 占位符只写短句，完整规则放在输入框下方的常驻提示里
 *
 * 原先把整句校验文案塞进 placeholder，卡片宽度不够时被省略号截断，用户看不到完整要求。
 * 登录侧不提长度：那里不校验（存量短密码用户不能被前端挡在门外），写了反而像门槛。
 */
const passwordPlaceholder = computed(() =>
  tab.value === 'register' ? registerPasswordPlaceholder : '请输入密码'
)

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
    <!-- 装饰层：漂浮纸片 + 批注圈，暗示“文档正在被批改” -->
    <div class="paper-float p1" aria-hidden="true">
      <span class="paper-line"></span><span class="paper-line"></span><span class="paper-line short"></span>
    </div>
    <div class="paper-float p2" aria-hidden="true">
      <span class="paper-line"></span><span class="paper-line short"></span><span class="paper-line"></span>
    </div>
    <span class="mark-circle m1" aria-hidden="true"></span>
    <span class="mark-circle m2" aria-hidden="true"></span>
    <span class="sparkle s1" aria-hidden="true"></span>
    <span class="sparkle s2" aria-hidden="true"></span>

    <!-- ============ 左：项目自我介绍 ============ -->
    <section class="hero">
      <!-- 品牌位：大 logo + 双色字标。原先这行是"文档 AI 处理平台"的说明，
           但说明交给下面的标题去做，这里只留品牌本身 -->
      <div class="hero-brand">
        <BrandMark :size="40" />
        <span class="hero-wordmark"><b>File</b><i>Agent</i></span>
      </div>

      <h1 class="hero-title">
        给文档配一条<br />
        <span class="hl">AI 流水线</span><span class="caret"></span>
      </h1>

      <p class="hero-desc">
        上传 <code>.docx</code> 或 <code>.pdf</code>——文字型直接提取，扫描版走云端识别——
        用一段你自己写的 Prompt 定义要做什么：批改、润色、摘要、翻译，转成
        <code>Markdown</code>，或把 PDF 整成一份可编辑的 <code>PPT</code>，走的都是同一条链路。
      </p>

      <ul class="hero-points">
        <li>
          <span class="point-icon">
            <!-- 你说标准：钢笔尖 -->
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
              <path d="M12 19l7-7 3 3-7 7-4 1 1-4Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
              <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
              <path d="M2 2l7.586 7.586" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <circle cx="11" cy="11" r="2" stroke="currentColor" stroke-width="2"/>
            </svg>
          </span>
          <span><b>你说标准</b><i>Prompt 就是处理规范，不套模板</i></span>
        </li>
        <li>
          <span class="point-icon">
            <!-- 它给结果：一份输入扇出多种产出 -->
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
              <path d="M2 12h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
              <path d="M8 12l4-6h8M8 12h10M8 12l4 6h8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <span><b>它给结果</b><i>批改给红字 diff，转换给 .md 与可编辑 .pptx</i></span>
        </li>
        <li>
          <span class="point-icon">
            <!-- 可追溯：放大镜 -->
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
              <circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/>
              <path d="M20 20l-3.5-3.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </span>
          <span><b>全程可追溯</b><i>意见带原文位置，还能召回你上传的校对规范</i></span>
        </li>
        <li>
          <span class="point-icon">
            <!-- 长文批得动：分层（一篇长文被切成多块并行处理） -->
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
              <path d="M12 3l9 5-9 5-9-5 9-5Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
              <path d="M3 13.5l9 5 9-5" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
            </svg>
          </span>
          <span><b>长文批得动</b><i>超预算自动分块批改，重试只补没成功的块</i></span>
        </li>
      </ul>
    </section>

    <!-- ============ 右：登录表单 ============ -->
    <!-- 品牌展示点统一交给左侧，卡片里不再放第二个 logo -->
    <div class="login-card">
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
              :placeholder="passwordPlaceholder"
              autocomplete="current-password"
              show-password
              @keyup.enter="handleSubmit"
            />
          </div>
          <!-- 完整规则常驻显示，不靠占位符：占位符一旦超出框宽就会被省略号截断 -->
          <p v-if="tab === 'register'" class="field-tip">{{ weakPasswordHint }}</p>
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

      <p v-if="tab === 'register'" class="card-footnote">
        注册即自动登录，并预置作文批改、文本润色、PDF 转 PPT、文档转 Markdown
        四个任务和一份通用校对规范
      </p>
    </div>
  </div>
</template>

<style scoped>
/* 全屏容器：左侧大字 + 右侧卡片；暖白底与光斑噪点由主题层透出 */
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: clamp(32px, 7vw, 110px);
  padding: 48px clamp(24px, 6vw, 96px);
  position: relative;
  overflow: hidden;
}

/* 稿纸横线：呼应“处理的是文档”，极淡，不抢正文 */
.login-page::before {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background: repeating-linear-gradient(
    to bottom,
    transparent 0 31px,
    color-mix(in oklab, var(--fa-ink) 4%, transparent) 31px 32px
  );
  mask-image: linear-gradient(to bottom, transparent, #000 22%, #000 78%, transparent);
}

/* ============ 左侧大字 ============ */
.hero {
  position: relative;
  z-index: 1;
  max-width: 560px;
  animation: fadeInUp var(--fa-duration-enter) var(--fa-ease-spring) both;
}

/* 品牌位：大 logo + 双色字标，取代原先那行"文档 AI 处理平台"的说明 */
.hero-brand {
  display: flex;
  align-items: center;
  gap: 14px;
  margin: 0 0 var(--space-xl);
  color: var(--fa-ink);
}

.hero-wordmark {
  font-size: var(--fa-text-2xl);
  letter-spacing: -0.03em;
  line-height: 1;
}

.hero-wordmark b {
  font-weight: 800;
  color: var(--fa-ink);
}

.hero-wordmark i {
  position: relative;
  z-index: 0;
  font-style: normal;
  font-weight: 700;
  padding: 0 3px;
  color: var(--fa-accent);
}

.hero-wordmark i::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 7px;
  z-index: -1;
  border-radius: 3px;
  background: color-mix(in oklab, var(--fa-accent-pop) 38%, transparent);
  transform: rotate(-1.4deg);
}

.hero-title {
  margin: 0 0 var(--space-lg);
  font-size: clamp(42px, 5.4vw, 72px);
  font-weight: 800;
  line-height: 1.08;
  letter-spacing: -0.02em;
  color: var(--el-text-color-primary);
}

/* 荧光笔划：从左往右拉出来，像刚批上去的一笔 */
.hl {
  position: relative;
  z-index: 0;
  padding: 0 6px;
  margin: 0 2px;
}

.hl::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 12%;
  height: 42%;
  z-index: -1;
  border-radius: 3px;
  background: color-mix(in oklab, var(--fa-accent-pop) 48%, transparent);
  transform: rotate(-1.1deg) scaleX(0);
  transform-origin: left center;
  animation: highlight 0.7s 0.35s cubic-bezier(0.2, 0.8, 0.2, 1) both;
}

@keyframes highlight {
  to {
    transform: rotate(-1.1deg) scaleX(1);
  }
}

/* 光标：AI 正在写的那一下，1.8s 一档与主题动效同拍 */
.caret {
  display: inline-block;
  width: 5px;
  height: 0.78em;
  margin-left: 10px;
  vertical-align: -0.04em;
  background: var(--fa-accent-pop);
  animation: caretBlink 1.8s steps(2, jump-none) infinite;
}

@keyframes caretBlink {
  0%,
  49% {
    opacity: 1;
  }
  50%,
  100% {
    opacity: 0;
  }
}

.hero-desc {
  margin: 0 0 var(--space-sm);
  max-width: 46ch;
  font-size: var(--fa-text-md);
  line-height: 1.85;
  color: var(--el-text-color-regular);
}

.hero-desc code {
  padding: 1px 6px;
  border-radius: 4px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: var(--fa-text-sm);
  color: var(--fa-ink);
  background: color-mix(in oklab, var(--fa-accent) 18%, var(--fa-bg));
}

/* 三条能力点：悬停往右让一下，交互不死板 */
.hero-points {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: var(--el-text-color-regular);
}

.hero-points li {
  display: flex;
  align-items: center;
  gap: 14px;
  transition: transform var(--fa-duration-fast) var(--fa-ease-spring);
}

.hero-points li:hover {
  transform: translateX(6px);
}

/* 每条能力点分两级：粗体小标题 + 灰色说明，避免四条一样重 */
.hero-points b {
  display: block;
  font-size: var(--fa-text-base);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.hero-points i {
  display: block;
  font-style: normal;
  font-size: var(--fa-text-sm);
  color: var(--el-text-color-secondary);
}

.point-icon {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 11px;
  color: var(--fa-ink);
  background: color-mix(in oklab, var(--fa-ink) 6%, var(--fa-bg));
  border: 1px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
}

/* ============ 右侧卡片 ============ */
/* 420px 是按"注册 tab 的完整口令提示要放得下"定的：380 时占位符与提示行都会被挤断 */
.login-card {
  width: 420px;
  flex-shrink: 0;
  padding: var(--space-xl) 34px;
  background: color-mix(in oklab, var(--fa-surface) 88%, transparent);
  -webkit-backdrop-filter: blur(24px);
  backdrop-filter: blur(24px);
  border-radius: var(--fa-radius-box);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
  box-shadow: var(--fa-shadow-lg);
  transition:
    box-shadow var(--fa-duration-fast) var(--fa-ease-spring),
    transform var(--fa-duration-fast) var(--fa-ease-spring);
  position: relative;
  z-index: 1;
  animation: fadeInUp var(--fa-duration-enter) 0.08s var(--fa-ease-spring) both;
}

.login-card:hover {
  box-shadow: var(--fa-shadow-xl);
  transform: translateY(-3px);
}

/* Tab 切换做成分段控件：整块轨道 + 墨色胶囊，比下划线更像"两种模式" */
:deep(.el-tabs__header) {
  margin-bottom: 26px;
}

:deep(.el-tabs__nav-wrap::after) {
  display: none;
}

:deep(.el-tabs__nav) {
  display: flex;
  width: 100%;
  padding: 4px;
  border-radius: var(--fa-radius-pill);
  background: color-mix(in oklab, var(--fa-ink) 6%, var(--fa-bg));
}

:deep(.el-tabs__active-bar) {
  display: none;
}

:deep(.el-tabs__item) {
  flex: 1;
  height: 36px;
  line-height: 36px;
  border-radius: var(--fa-radius-pill);
  font-weight: 600;
  color: var(--el-text-color-secondary);
  transition:
    background var(--fa-duration-fast) var(--fa-ease-spring),
    color var(--fa-duration-fast) var(--fa-ease-spring);
}

:deep(.el-tabs__item.is-active) {
  color: var(--fa-surface);
  background: var(--fa-ink);
}

/* 输入框前缀图标 */
.input-with-icon {
  position: relative;
  width: 100%;
}

.input-icon {
  position: absolute;
  left: 14px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--el-text-color-placeholder);
  z-index: 1;
  pointer-events: none;
  display: flex;
  align-items: center;
}

/* 字段之间留够呼吸：错误红字与常驻提示都挤在同一格里 */
:deep(.el-form-item) {
  margin-bottom: 22px;
}

:deep(.el-form-item__label) {
  padding-bottom: 6px;
  font-size: var(--fa-text-sm);
  font-weight: 600;
  color: var(--fa-ink);
}

:deep(.el-input__wrapper) {
  min-height: 46px;
  padding: 6px 14px 6px 38px;
  border-radius: var(--fa-radius-card);
  transition: box-shadow var(--fa-duration-fast) ease-out;
}

:deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px color-mix(in oklab, var(--fa-ink) 22%, transparent) inset;
}

:deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1.5px var(--fa-ink) inset !important;
}

/* 提交按钮：全圆胶囊 + 近黑，渐变外观交给主题层 */
.submit-btn {
  width: 100%;
  height: 46px;
  font-size: var(--fa-text-base);
  font-weight: 600;
  letter-spacing: 0.5px;
  transition:
    transform var(--fa-duration-fast) var(--fa-ease-spring),
    box-shadow var(--fa-duration-fast) var(--fa-ease-spring);
}

.submit-btn:hover {
  transform: translateY(-1px);
  box-shadow: var(--fa-shadow-lg);
}

/* 占位符之外的常驻说明：短、次要色，不跟校验红字抢层级 */
.field-tip {
  width: 100%;
  margin: 6px 0 0;
  font-size: var(--fa-text-xs);
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

/* 已经报错就别再叠一条灰字说明：同一格里两句提示只会互相淹掉 */
.login-card :deep(.el-form-item.is-error .field-tip) {
  display: none;
}

.card-footnote {
  margin: 6px 0 0;
  padding-top: 16px;
  border-top: 1px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
  font-size: var(--fa-text-xs);
  line-height: 1.75;
  color: var(--el-text-color-secondary);
}

/* ============ 装饰层：漂浮纸片 + 批注圈 + 星点 ============ */
@keyframes paperFloat {
  0%,
  100% {
    transform: translateY(0) rotate(var(--rot, -6deg));
  }
  50% {
    transform: translateY(-16px) rotate(calc(var(--rot, -6deg) + 2.5deg));
  }
}

.paper-float {
  position: absolute;
  width: 88px;
  height: 110px;
  padding: 17px 13px;
  display: flex;
  flex-direction: column;
  gap: 9px;
  background: color-mix(in oklab, var(--fa-surface) 78%, transparent);
  border: 1px solid color-mix(in oklab, var(--fa-ink) 8%, transparent);
  border-radius: 10px;
  box-shadow: var(--fa-shadow-lg);
  opacity: 0.75;
  pointer-events: none;
  z-index: 0;
  animation: paperFloat 8s ease-in-out infinite;
}

/* 折角：右上角三角缺口，一眼认出是“一页纸” */
.paper-float::after {
  content: '';
  position: absolute;
  top: -1px;
  right: -1px;
  width: 21px;
  height: 21px;
  border-radius: 0 10px 0 8px;
  background: var(--fa-bg);
  border-left: 1px solid color-mix(in oklab, var(--fa-ink) 10%, transparent);
  border-bottom: 1px solid color-mix(in oklab, var(--fa-ink) 10%, transparent);
}

.paper-line {
  height: 6px;
  border-radius: 3px;
  background: color-mix(in oklab, var(--fa-ink) 14%, transparent);
}

.paper-line.short {
  width: 62%;
  background: color-mix(in oklab, var(--fa-accent) 60%, transparent);
}

.p1 {
  --rot: -9deg;
  top: 12%;
  right: 8%;
}

.p2 {
  --rot: 7deg;
  bottom: 10%;
  left: 6%;
  animation-duration: 10s;
  animation-delay: -3.5s;
}

.mark-circle {
  position: absolute;
  border: 2.5px solid color-mix(in oklab, var(--fa-accent-pop) 70%, transparent);
  border-radius: 50% 45% 55% 48% / 48% 55% 45% 52%;
  pointer-events: none;
  z-index: 0;
  animation: paperFloat 11s ease-in-out infinite;
}

.m1 {
  width: 74px;
  height: 58px;
  left: 44%;
  bottom: 14%;
  opacity: 0.5;
}

.m2 {
  width: 46px;
  height: 38px;
  right: 30%;
  top: 14%;
  opacity: 0.4;
  animation-delay: -5s;
}

/* 星点：AI 的“灵光一下”，纯 CSS 四角星（不依赖字形，跨平台一致） */
.sparkle {
  position: absolute;
  background: var(--fa-accent-pop);
  clip-path: polygon(50% 0, 62% 38%, 100% 50%, 62% 62%, 50% 100%, 38% 62%, 0 50%, 38% 38%);
  pointer-events: none;
  z-index: 0;
  animation: sparkleFade 5s ease-in-out infinite;
}

.s1 {
  width: 22px;
  height: 22px;
  left: 38%;
  top: 22%;
}

.s2 {
  width: 15px;
  height: 15px;
  right: 12%;
  bottom: 26%;
  animation-delay: -2.4s;
}

@keyframes sparkleFade {
  0%,
  100% {
    opacity: 0.15;
    transform: scale(0.8) rotate(0deg);
  }
  50% {
    opacity: 0.75;
    transform: scale(1.1) rotate(20deg);
  }
}

/* ============ 响应式 ============ */
/* 窄屏改为上下堆叠：左侧介绍仍然给到，只是缩小，不把内容直接藏掉 */
@media (max-width: 768px) {
  /* 手机端第一屏就是登录表单：卡片排到 hero 前，营销文案收掉只留标题 */
  .login-page {
    gap: 20px;
    padding: 32px 16px 48px;
  }

  .login-card {
    order: -1;
  }

  .hero-desc,
  .hero-points {
    display: none;
  }

  .hero-title {
    font-size: 28px;
    margin-bottom: 0;
  }
}

@media (max-width: 1000px) {
  .login-page {
    flex-direction: column;
    justify-content: flex-start;
    gap: 40px;
    padding: 56px 24px 48px;
    overflow-x: hidden;
  }

  .hero {
    max-width: 100%;
    text-align: left;
  }

  .hero-title {
    font-size: clamp(34px, 7.6vw, 48px);
  }

  .hero-desc {
    font-size: var(--fa-text-base);
    margin-bottom: var(--space-lg);
  }

  .p1,
  .m1,
  .s1 {
    display: none;
  }
}

@media (max-width: 480px) {
  .paper-float,
  .mark-circle,
  .sparkle {
    display: none;
  }

  .login-card {
    width: 100%;
    max-width: 360px;
    padding: var(--space-lg);
  }

  .hero-points {
    font-size: var(--fa-text-sm);
  }
}

@media (prefers-reduced-motion: reduce) {
  .paper-float,
  .mark-circle,
  .sparkle,
  .caret,
  .hl::before {
    animation: none;
  }

  .hl::before {
    transform: rotate(-1.1deg) scaleX(1);
  }
}
</style>
