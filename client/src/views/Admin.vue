<script setup lang="ts">
/**
 * 管理页（SPEC §9.11）—— 用户列表 / 角色升降 / 默认模型授权开关
 *
 * - 入口和路由都只对 admin 展示/放行，真正的权限把关在后端 require_admin；
 * - 自己的行：角色下拉禁用（后端也拒绝，防自锁双保险）；
 * - 授权开关立即生效于"下一次处理"，正在后台跑的任务不受影响（§9.11.3-3）。
 */
import { onActivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { adminApi } from '@/api/admin'
import { extractErrorMessage } from '@/api/client'
import type { AdminUserRow, UserRole } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const users = ref<AdminUserRow[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const resp = await adminApi.listUsers()
    users.value = resp.data
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(load)
onActivated(load)

async function changeRole(row: AdminUserRow, role: UserRole) {
  try {
    const resp = await adminApi.updateRole(row.id, role)
    Object.assign(row, resp.data)
    ElMessage.success(`${row.username} 角色已更新为 ${role}`)
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
    await load() // 失败回滚 UI 显示
  }
}

async function toggleAccess(row: AdminUserRow, value: boolean) {
  try {
    const resp = await adminApi.updateDefaultAccess(row.id, value)
    Object.assign(row, resp.data)
    ElMessage.success(value ? `已授权 ${row.username} 使用默认模型` : `已吊销 ${row.username} 的默认模型授权`)
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
    await load()
  }
}

function formatDate(raw: string): string {
  return new Date(raw).toLocaleDateString('zh-CN')
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div class="page-header-content">
        <div class="page-icon">
          <!-- 用户管理：人群图标，与导航入口一致 -->
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M17 21V19C17 16.79 14.76 15 12 15C9.24 15 7 16.79 7 19V21" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <circle cx="12" cy="7" r="4" stroke="currentColor" stroke-width="2"/>
            <path d="M20 21V19C20 17.5 19 16.2 17.5 15.4M4 21V19C4 17.5 5 16.2 6.5 15.4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
        </div>
        <div>
          <h2 class="page-title">用户管理</h2>
          <p class="page-subtitle">
            升降管理员角色、控制谁能使用官方默认模型。角色与授权变更在对方下次进入应用时即生效，无需重新登录；
            授权吊销不打断正在处理的任务
          </p>
        </div>
      </div>
    </div>

    <div class="table-wrapper">
      <el-table v-loading="loading" :data="users" stripe>
        <template #empty>
          <el-empty description="暂无用户数据" :image-size="80" />
        </template>
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column label="角色" width="140">
          <template #default="{ row }">
            <el-select
              :model-value="row.role"
              :disabled="row.username === auth.username"
              size="small"
              @update:model-value="(v: unknown) => changeRole(row as AdminUserRow, v as UserRole)"
            >
              <el-option label="普通用户" value="user" />
              <el-option label="管理员" value="admin" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="默认模型授权" width="130" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="row.can_use_default"
              @update:model-value="
                (v: unknown) => toggleAccess(row as AdminUserRow, Boolean(v))
              "
            />
          </template>
        </el-table-column>
        <el-table-column prop="task_count" label="任务数" width="90" align="center" />
        <el-table-column prop="log_count" label="处理次数" width="90" align="center" />
        <el-table-column prop="total_tokens" label="token 累计" width="120" align="center" />
        <el-table-column label="注册于" width="120">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.page-header {
  margin-bottom: 16px;
}
.page-title {
  margin: 0 0 4px;
  font-size: 18px;
}
.page-subtitle {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  max-width: 720px;
}
.table-wrapper {
  border-radius: var(--fa-radius-field);
  overflow: hidden;
}
</style>
