import { useEffect } from 'react'
import { useParams } from 'react-router-dom'

import { rememberKnowledgeBase } from '@/lib/lastKnowledgeBase'
import { ContentListPane } from './components/ContentListPane'
import { KnowledgeBaseListPane } from './components/KnowledgeBaseListPane'
import { QaPanel } from './components/QaPanel'
import styles from './index.module.scss'

export default function KnowledgeBasePage() {
  const { id } = useParams<{ id: string }>()

  useEffect(() => {
    if (id) rememberKnowledgeBase(id)
  }, [id])

  return (
    <div className={styles.page}>
      <KnowledgeBaseListPane selectedId={id} />
      <ContentListPane kbId={id} />
      <QaPanel />
    </div>
  )
}
