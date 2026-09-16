import { useEffect, useLayoutEffect } from 'react'
import { useParams } from 'react-router-dom'

import { rememberKnowledgeBase } from '@/lib/lastKnowledgeBase'
import { useDocumentWindows } from '@/hooks/useDocumentWindows'
import { ContentListPane } from './components/ContentListPane'
import { DocumentWindow } from './components/DocumentWindow'
import { KnowledgeBaseListPane } from './components/KnowledgeBaseListPane'
import { QaPanel } from './components/QaPanel'
import styles from './index.module.scss'

export default function KnowledgeBasePage() {
  const { id } = useParams<{ id: string }>()
  const { windows, open, focus, close, clear } = useDocumentWindows()

  useEffect(() => {
    if (id) rememberKnowledgeBase(id)
  }, [id])

  // 切换知识库清空所有浮动窗口
  useLayoutEffect(() => {
    clear()
  }, [id, clear])

  return (
    <div className={styles.page}>
      <KnowledgeBaseListPane selectedId={id} />
      <ContentListPane kbId={id} onOpenDocument={open} onCloseDocument={close} />
      <QaPanel />
      {windows.map((window) => (
        <DocumentWindow
          key={window.documentId}
          documentId={window.documentId}
          zIndex={window.zIndex}
          offset={window.offset}
          onClose={close}
          onFocus={focus}
        />
      ))}
    </div>
  )
}
