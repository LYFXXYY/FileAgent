/** 任务 CRUD */

import { http } from './client'
import type { Task, TaskCreatePayload, TaskUpdatePayload } from './types'

export const taskApi = {
  list() {
    return http.get<Task[]>('/tasks')
  },
  create(payload: TaskCreatePayload) {
    return http.post<Task>('/tasks', payload)
  },
  update(id: number, payload: TaskUpdatePayload) {
    return http.put<Task>(`/tasks/${id}`, payload)
  },
  remove(id: number) {
    return http.delete(`/tasks/${id}`)
  },
}
