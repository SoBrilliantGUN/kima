import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  getKnowledgeBase,
  listKnowledgeBases,
  listKbContents,
  updateKnowledgeBase,
} from '@/api/knowledgeBases'
import type { KnowledgeBaseCreate, KnowledgeBaseUpdate } from '@/api/types'
import { KNOWLEDGE_BASES } from './common'

// 分页每页数量
const PAGE_SIZE = 50

/**
 * 获取知识库列表（无限滚动分页）。
 * 以已加载条数作为下一页的游标，配合后端的 offset 分页。
 */
export function useKnowledgeBases() {
  return useInfiniteQuery({
    queryKey: [KNOWLEDGE_BASES],
    queryFn: ({ pageParam }) => listKnowledgeBases(PAGE_SIZE, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0)
      // 已加载数量小于总数时返回下一游标，否则返回 undefined 表示没有更多页
      return loaded < lastPage.total ? loaded : undefined
    },
  })
}

/**
 * 获取单个知识库详情。
 * id 为 undefined 时禁用查询（例如路由参数尚未就绪）。
 */
export function useKnowledgeBase(id: string | undefined) {
  return useQuery({
    queryKey: [KNOWLEDGE_BASES, id],
    queryFn: () => getKnowledgeBase(id!),
    enabled: id !== undefined,
  })
}

/** 创建知识库，成功后使列表缓存失效以触发重新拉取 */
export function useCreateKnowledgeBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: KnowledgeBaseCreate) => createKnowledgeBase(payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] }),
  })
}

/** 更新指定知识库，成功后使列表缓存失效 */
export function useUpdateKnowledgeBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: KnowledgeBaseUpdate }) =>
      updateKnowledgeBase(id, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] }),
  })
}

/** 删除指定知识库，成功后使列表缓存失效 */
export function useDeleteKnowledgeBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteKnowledgeBase(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] }),
  })
}

/** 获取某知识库的内容列表（当前含关联笔记，模块 4 增文档）。 */
export function useKbContents(kbId: string | undefined) {
  return useQuery({
    queryKey: [KNOWLEDGE_BASES, kbId, 'contents'],
    queryFn: () => listKbContents(kbId!),
    enabled: kbId !== undefined,
  })
}
