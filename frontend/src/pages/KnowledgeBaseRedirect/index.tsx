import { Navigate } from 'react-router-dom'

import { useKnowledgeBases } from '@/hooks/useKnowledgeBases'
import { recallKnowledgeBase } from '@/lib/lastKnowledgeBase'

export default function KnowledgeBaseRedirect() {
  const { data, isLoading } = useKnowledgeBases()

  if (isLoading || !data) {
    return null
  }

  const target = recallKnowledgeBase() ?? data.pages[0]?.items[0]?.id
  if (!target) {
    // 理论上不会发生：后端启动时预置了「我的知识库」
    return null
  }

  return <Navigate to={`/knowledge-bases/${target}`} replace />
}
