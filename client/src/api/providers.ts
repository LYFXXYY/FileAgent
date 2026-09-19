/** 提供商 CRUD */

import { http } from './client'
import type {
  Provider,
  ProviderCreatePayload,
  ProviderUpdatePayload,
} from './types'

export const providerApi = {
  list() {
    return http.get<Provider[]>('/providers')
  },
  create(payload: ProviderCreatePayload) {
    return http.post<Provider>('/providers', payload)
  },
  update(id: number, payload: ProviderUpdatePayload) {
    return http.put<Provider>(`/providers/${id}`, payload)
  },
  remove(id: number) {
    return http.delete(`/providers/${id}`)
  },
}
