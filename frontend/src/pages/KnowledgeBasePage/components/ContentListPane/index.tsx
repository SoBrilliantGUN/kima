import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { Document, DocumentStatus } from '@/api/types'
import { FilePlusIcon, MessageCircleIcon, SearchIcon } from '@/components/icons'
import { useDeleteDocument, useRetryDocument } from '@/hooks/useDocuments'
import { useKnowledgeBase, useKbContents } from '@/hooks/useKnowledgeBases'
import { AddContentMenu } from './components/AddContentMenu'
import { DocumentDropzone } from './components/DocumentDropzone'
import { UrlInputModal } from './components/UrlInputModal'
import styles from './index.module.scss'

const STATUS_LABEL: Record<DocumentStatus, string> = {
  pending: '等待中',
  processing: '解析中',
  done: '已完成',
  error: '失败',
}

interface ContentListPaneProps {
  kbId?: string
  qaOpen: boolean
  onOpenQa: () => void
  onOpenDocument: (documentId: string) => void
  onCloseDocument: (documentId: string) => void
}

export function ContentListPane({
  kbId,
  qaOpen,
  onOpenQa,
  onOpenDocument,
  onCloseDocument,
}: ContentListPaneProps) {
  const { data: kb, isLoading, isError } = useKnowledgeBase(kbId)
  const { data: contents } = useKbContents(kbId)
  const deleteDocument = useDeleteDocument()
  const retryDocument = useRetryDocument()
  const navigate = useNavigate()

  const [menuOpen, setMenuOpen] = useState(false)
  const [dropzoneOpen, setDropzoneOpen] = useState(false)
  const [urlModalOpen, setUrlModalOpen] = useState(false)

  const items = contents?.items ?? []

  function handleSelect(type: 'url' | 'document') {
    setMenuOpen(false)
    if (!kbId) return
    if (type === 'url') setUrlModalOpen(true)
    else setDropzoneOpen(true)
  }

  async function handleDeleteDocument(document: Document) {
    if (!window.confirm(`确定删除文档「${document.title}」吗？该操作不可恢复。`)) return
    await deleteDocument.mutateAsync(document.id)
    onCloseDocument(document.id)
  }

  if (isLoading) {
    return <section className={styles.pane}>加载中…</section>
  }
  if (isError || !kb) {
    return <section className={styles.pane}>知识库不存在或已删除。</section>
  }

  return (
    <section className={styles.pane}>
      <header className={styles.header}>
        <span className={styles.cover} style={{ backgroundColor: kb.color }}>
          {kb.name.charAt(0)}
        </span>
        <div className={styles.heading}>
          <h2 className={styles.name}>{kb.name}</h2>
          {kb.description ? <p className={styles.description}>{kb.description}</p> : null}
        </div>
        {!qaOpen ? (
          <button type="button" className={styles.askButton} onClick={onOpenQa}>
            <MessageCircleIcon />
            问知识库
          </button>
        ) : null}
      </header>

      <div className={styles.toolbar}>
        <div className={styles.addWrap}>
          <button
            type="button"
            className={styles.addButton}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <FilePlusIcon />
            添加内容
          </button>
          {menuOpen ? (
            <AddContentMenu onSelect={handleSelect} onClose={() => setMenuOpen(false)} />
          ) : null}
        </div>
        <button type="button" className={styles.iconButton} aria-label="查询" title="查询">
          <SearchIcon />
        </button>
      </div>

      {items.length === 0 ? (
        <div className={styles.empty}>
          <p>知识库里什么也没有</p>
          <p className={styles.emptyHint}>点「添加内容」导入文档</p>
        </div>
      ) : (
        <ul className={styles.list}>
          {items.map((item) => {
            if (item.type === 'note' && item.note) {
              return (
                <li key={item.note.id} className={styles.itemRow}>
                  <button
                    type="button"
                    className={styles.item}
                    onClick={() => navigate(`/notes/${item.note!.id}`)}
                  >
                    <span className={styles.itemTitle}>{item.note.title}</span>
                    <span className={styles.itemSummary}>笔记</span>
                  </button>
                </li>
              )
            }
            if (item.type === 'document' && item.document) {
              const document = item.document
              return (
                <li key={document.id} className={styles.itemRow}>
                  <button
                    type="button"
                    className={styles.item}
                    onClick={() => onOpenDocument(document.id)}
                  >
                    <span className={styles.itemTitle}>{document.title}</span>
                    <span className={styles.itemSummary}>
                      <span className={`${styles.badge} ${styles[document.status]}`}>
                        {STATUS_LABEL[document.status]}
                      </span>
                      {document.source_type}
                    </span>
                  </button>
                  {document.status === 'error' ? (
                    <div className={styles.itemActions}>
                      <button
                        type="button"
                        onClick={() => void retryDocument.mutateAsync(document.id)}
                      >
                        重试
                      </button>
                      <button type="button" onClick={() => void handleDeleteDocument(document)}>
                        删除
                      </button>
                    </div>
                  ) : null}
                </li>
              )
            }
            return null
          })}
        </ul>
      )}

      {dropzoneOpen && kbId ? (
        <DocumentDropzone kbId={kbId} onClose={() => setDropzoneOpen(false)} />
      ) : null}
      {urlModalOpen && kbId ? (
        <UrlInputModal kbId={kbId} onClose={() => setUrlModalOpen(false)} />
      ) : null}
    </section>
  )
}
