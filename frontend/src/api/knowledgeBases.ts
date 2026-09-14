import { api } from '@/api/client'
import type {
  ContentListResponse,
  KnowledgeBase,
  KnowledgeBaseCreate,
  KnowledgeBaseListResponse,
  KnowledgeBaseUpdate,
} from '@/api/types'

export function listKnowledgeBases(limit = 50, offset = 0): Promise<KnowledgeBaseListResponse> {
  return api.get<KnowledgeBaseListResponse>(`/api/knowledge-bases?limit=${limit}&offset=${offset}`)
}

export function getKnowledgeBase(id: string): Promise<KnowledgeBase> {
  return api.get<KnowledgeBase>(`/api/knowledge-bases/${id}`)
}

export function createKnowledgeBase(payload: KnowledgeBaseCreate): Promise<KnowledgeBase> {
  return api.post<KnowledgeBase>('/api/knowledge-bases', payload)
}

export function updateKnowledgeBase(
  id: string,
  payload: KnowledgeBaseUpdate,
): Promise<KnowledgeBase> {
  return api.patch<KnowledgeBase>(`/api/knowledge-bases/${id}`, payload)
}

export function deleteKnowledgeBase(id: string): Promise<void> {
  return api.delete<void>(`/api/knowledge-bases/${id}`)
}

export function listKbContents(id: string): Promise<ContentListResponse> {
  return api.get<ContentListResponse>(`/api/knowledge-bases/${id}/contents`)
}
