import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { deleteConversation, listConversations } from '@/api/chat'

const CONVERSATIONS = 'conversations'

/** 会话列表（首页全局 kb_id 空 / 右面板按库）。 */
export function useConversations(kbId?: string | null) {
  return useQuery({
    queryKey: [CONVERSATIONS, kbId ?? 'global'],
    queryFn: () => listConversations(kbId),
  })
}

/** 删除会话，成功后失效列表。 */
export function useDeleteConversation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [CONVERSATIONS] }),
  })
}
