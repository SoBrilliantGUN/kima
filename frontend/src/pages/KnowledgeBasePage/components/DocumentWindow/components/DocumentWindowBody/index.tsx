import { documentFileUrl } from '@/api/documents'
import type { Document } from '@/api/types'
import { useDocumentContent } from '@/hooks/useDocuments'
import { MarkdownView } from './components/MarkdownView'
import styles from './index.module.scss'

interface DocumentWindowBodyProps {
  document: Document | undefined
  isLoading: boolean
}

export function DocumentWindowBody({ document, isLoading }: DocumentWindowBodyProps) {
  // word/url 完成后阅读解析出的 markdown；pdf 走 iframe 原文件
  const needsMarkdown =
    document?.status === 'done' &&
    (document.source_type === 'word' || document.source_type === 'url')
  const { data: content } = useDocumentContent(document?.id, needsMarkdown)

  return (
    <div className={styles.body}>
      {isLoading ? (
        <div className={styles.placeholder}>加载中…</div>
      ) : !document ? (
        <div className={styles.placeholder}>文档不存在或已删除。</div>
      ) : document.status === 'pending' || document.status === 'processing' ? (
        <div className={styles.placeholder}>文档解析中，请稍候…</div>
      ) : document.status === 'error' ? (
        <div className={styles.placeholder}>
          <p>解析失败</p>
          {document.error_message ? (
            <p className={styles.errorText}>{document.error_message}</p>
          ) : null}
        </div>
      ) : document.source_type === 'pdf' ? (
        <iframe
          className={styles.frame}
          src={documentFileUrl(document.id)}
          title={document.title}
        />
      ) : content ? (
        content.markdown.trim() ? (
          <MarkdownView markdown={content.markdown} />
        ) : (
          <div className={styles.placeholder}>无正文内容</div>
        )
      ) : (
        <div className={styles.placeholder}>正文加载中…</div>
      )}
    </div>
  )
}
