import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createDocumentFromUrl,
  deleteDocument,
  getDocument,
  getDocumentContent,
  retryDocument,
  uploadDocument,
} from '@/api/documents'
import type { DocumentCreateFromUrl } from '@/api/types'
import { KNOWLEDGE_BASES } from './common'

// 文档查询键前缀；详情与列表共用
const DOCUMENTS = 'documents'

// pending/processing 时轮询间隔（毫秒）
const POLL_INTERVAL = 2000

/** 获取单个文档详情；处理中轮询，done/error 停止。 */
export function useDocument(id: string | undefined) {
  return useQuery({
    queryKey: [DOCUMENTS, id],
    queryFn: () => getDocument(id!),
    enabled: id !== undefined,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'pending' || status === 'processing' ? POLL_INTERVAL : false
    },
  })
}

/** 获取文档 markdown 正文；仅文档完成且需要阅读时启用。 */
export function useDocumentContent(id: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: [DOCUMENTS, id, 'content'],
    queryFn: () => getDocumentContent(id!),
    enabled: id !== undefined && enabled,
  })
}

/** 上传本地文档（pdf/word）。 */
export function useUploadDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ kbId, file }: { kbId: string; file: File }) => uploadDocument(kbId, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] })
    },
  })
}

/** 从 URL 创建文档。 */
export function useCreateDocumentFromUrl() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: DocumentCreateFromUrl) => createDocumentFromUrl(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] })
    },
  })
}

/** 重试失败的文档。 */
export function useRetryDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => retryDocument(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] })
      queryClient.invalidateQueries({ queryKey: [DOCUMENTS] })
    },
  })
}

/** 删除文档。 */
export function useDeleteDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteDocument(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] })
      queryClient.invalidateQueries({ queryKey: [DOCUMENTS] })
    },
  })
}
