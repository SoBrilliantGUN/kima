import { documentFileUrl } from '@/api/documents'
import type { DocumentStatus } from '@/api/types'
import { CloseIcon } from '@/components/icons'
import { useDeleteDocument, useDocument, useRetryDocument } from '@/hooks/useDocuments'
import { useWindowRect } from '@/hooks/useWindowRect'
import { DocumentWindowBody } from './components/DocumentWindowBody'
import { ResizeHandles } from './components/ResizeHandles'
import styles from './index.module.scss'

const STATUS_LABEL: Record<DocumentStatus, string> = {
  pending: '等待中',
  processing: '解析中',
  done: '已完成',
  error: '失败',
}

interface DocumentWindowProps {
  documentId: string
  zIndex: number
  offset: number
  onClose: (documentId: string) => void
  onFocus: (documentId: string) => void
}

export function DocumentWindow({
  documentId,
  zIndex,
  offset,
  onClose,
  onFocus,
}: DocumentWindowProps) {
  const { data: document, isLoading } = useDocument(documentId)
  const retry = useRetryDocument()
  const deleteDocument = useDeleteDocument()
  const { style, headerHandlers, resizeHandlers } = useWindowRect(offset)

  async function handleDelete() {
    if (!window.confirm(`确定删除文档「${document?.title ?? ''}」吗？该操作不可恢复。`)) return
    await deleteDocument.mutateAsync(documentId)
    onClose(documentId)
  }

  return (
    <section
      className={styles.window}
      style={{ ...style, zIndex }}
      onPointerDown={() => onFocus(documentId)}
    >
      <header className={styles.header} {...headerHandlers}>
        <div className={styles.titleArea}>
          <h2 className={styles.title}>{document?.title ?? '文档'}</h2>
          {document ? (
            <div className={styles.meta}>
              <span className={styles.type}>{document.source_type}</span>
              <span className={`${styles.badge} ${styles[document.status]}`}>
                {STATUS_LABEL[document.status]}
              </span>
            </div>
          ) : null}
        </div>
        <div className={styles.actions}>
          {document?.status === 'error' ? (
            <button type="button" onClick={() => void retry.mutateAsync(documentId)}>
              重试
            </button>
          ) : null}
          {document?.status === 'done' && document.source_type === 'url' ? (
            <a href={document.source_url ?? '#'} target="_blank" rel="noreferrer">
              打开原网页
            </a>
          ) : null}
          {document?.status === 'done' &&
          (document.source_type === 'pdf' || document.source_type === 'word') ? (
            <a href={documentFileUrl(document.id)} download>
              下载原文件
            </a>
          ) : null}
          {document ? (
            <button type="button" onClick={() => void handleDelete()}>
              删除
            </button>
          ) : null}
          <button
            type="button"
            className={styles.close}
            onClick={() => onClose(documentId)}
            aria-label="关闭"
          >
            <CloseIcon />
          </button>
        </div>
      </header>

      <DocumentWindowBody document={document} isLoading={isLoading} />

      <ResizeHandles
        onPointerDown={resizeHandlers.onPointerDown}
        onPointerMove={resizeHandlers.onPointerMove}
        onPointerEnd={resizeHandlers.onPointerEnd}
      />
    </section>
  )
}
