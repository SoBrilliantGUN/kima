import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  addNoteToKnowledgeBase,
  createNote,
  createNoteFromUrl,
  deleteNote,
  getNote,
  listNotes,
  updateNote,
} from '@/api/notes'
import type {
  NoteAddToKnowledgeBase,
  NoteCreate,
  NoteCreateFromUrl,
  NoteUpdate,
} from '@/api/types'
import { KNOWLEDGE_BASES } from './common'

// 笔记查询键的公共前缀；列表与详情共用
const NOTES = 'notes'
const PAGE_SIZE = 50

/** 获取笔记列表（无限滚动分页）。 */
export function useNotes() {
  return useInfiniteQuery({
    queryKey: [NOTES],
    queryFn: ({ pageParam }) => listNotes(PAGE_SIZE, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0)
      return loaded < lastPage.total ? loaded : undefined
    },
  })
}

/** 获取单条笔记详情，id 未就绪时禁用。 */
export function useNote(id: string | undefined) {
  return useQuery({
    queryKey: [NOTES, id],
    queryFn: () => getNote(id!),
    enabled: id !== undefined,
  })
}

/** 从 URL 采集网页生成笔记。 */
export function useCreateNoteFromUrl() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: NoteCreateFromUrl) => createNoteFromUrl(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [NOTES] })
      queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] })
    },
  })
}

/** 新建空白笔记。 */
export function useCreateNote() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: NoteCreate) => createNote(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [NOTES] })
      queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] })
    },
  })
}

/** 更新笔记标题/正文（自动保存）。只失效列表，不触碰详情缓存以免重置编辑器。 */
export function useUpdateNote() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: NoteUpdate }) => updateNote(id, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [NOTES] }),
  })
}

/** 删除笔记，成功后失效列表与知识库内容（清关联）。 */
export function useDeleteNote() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteNote(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [NOTES] })
      queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] })
    },
  })
}

/** 把笔记添加到知识库（幂等）。 */
export function useAddNoteToKnowledgeBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: NoteAddToKnowledgeBase }) =>
      addNoteToKnowledgeBase(id, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [KNOWLEDGE_BASES] }),
  })
}
