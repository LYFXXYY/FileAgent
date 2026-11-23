<script setup lang="ts">
/**
 * 自助改密弹窗
 *
 * 挂在 AppLayout 上（桌面在用户名旁、手机端在"更多"弹层里），因为它是账号级动作，
 * 不属于任何业务页。
 *
 * 【为什么成功后要重新登录】
 * JWT 无状态、也没有 token 版本列，服务端无法让别处已签发的 token 立刻失效。
 * 前端清掉本地 token 跳登录页，等价于踢掉"自己当前这个会话"；要做到全端即时失效
 * 得给 users 加版本戳并在鉴权依赖里比对，那是改表（AGENTS §5），这里刻意没做。
 */
import { reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { authApi } from '@/api/auth'
import { extractErrorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import {
  PASSWORD_MAX,
  PASSWORD_MIN,
  checkPasswordStrength,
  makeConfirmValidator,
  passwordPlaceholder,
  weakPasswordHint,
} from '@/utils/passwordPolicy'

const visible = defineModel<boolean>({ required: true })

const auth = useAuthStore()
const router = useRouter()
const formRef = ref<FormInstance>()
const loading = ref(false)

const form = reactive({ old_password: '', new_password: '', confirm: '' })

const rules: FormRules = {
  old_password: [{ required: true, message: '请输入原密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: PASSWORD_MIN, max: PASSWORD_MAX, message: weakPasswordHint, trigger: 'blur' },
    { validator: checkPasswordStrength, trigger: 'blur' },
  ],
  confirm: [{ validator: makeConfirmValidator(() => form.new_password), trigger: 'blur' }],
}

// 每次打开都从干净状态开始：留着上次的输入会让人以为已经填过
watch(visible, (open) => {
  if (!open) return
  form.old_password = ''
  form.new_password = ''
  form.confirm = ''
  formRef.value?.clearValidate()
})

async function submit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await authApi.changePassword(form.old_password, form.new_password)
    visible.value = false
    ElMessage.success('密码已更新，请用新密码重新登录')
    // 后端不返回 token，旧 token 也没被吊销，唯一诚实的收尾就是重新登一次
    auth.logout()
    router.replace('/login')
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="修改密码" width="460px" class="pwd-dialog">
    <p class="dialog-note">
      改成功后当前会话会被退出，请用新密码重新登录。已登录的其他设备要等它的
      token 自然过期（默认 2 小时）——本项目没有服务端会话表，做不到即时全端失效。
    </p>

    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <el-form-item label="原密码" prop="old_password">
        <el-input
          v-model="form.old_password"
          type="password"
          placeholder="请输入原密码"
          autocomplete="current-password"
          show-password
        />
      </el-form-item>
      <el-form-item label="新密码" prop="new_password">
        <el-input
          v-model="form.new_password"
          type="password"
          :placeholder="passwordPlaceholder"
          autocomplete="new-password"
          show-password
        />
        <!-- 完整规则常驻说明；报错时由 CSS 收起，不与红字挤在同一格 -->
        <p class="field-tip">{{ weakPasswordHint }}</p>
      </el-form-item>
      <el-form-item label="确认新密码" prop="confirm">
        <el-input
          v-model="form.confirm"
          type="password"
          placeholder="再输入一次新密码"
          autocomplete="new-password"
          show-password
          @keyup.enter="submit"
        />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="submit">确认修改</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-note {
  margin: 0 0 18px;
  font-size: var(--fa-text-xs);
  line-height: 1.75;
  color: var(--el-text-color-secondary);
}

.field-tip {
  width: 100%;
  margin: 6px 0 0;
  font-size: var(--fa-text-xs);
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.pwd-dialog :deep(.el-form-item.is-error .field-tip) {
  display: none;
}
</style>
