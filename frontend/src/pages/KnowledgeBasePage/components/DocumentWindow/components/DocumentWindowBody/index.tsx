import { documentFileUrl } from '@/api/documents'
import type { Document } from '@/api/types'
import styles from './index.module.scss'

interface DocumentWindowBodyProps {
  document: Document | undefined
  isLoading: boolean
}

export function DocumentWindowBody({ document, isLoading }: DocumentWindowBodyProps) {
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
      ) : document.source_type === 'word' ? (
        <div className={styles.placeholder}>
          <a className={styles.linkButton} href={documentFileUrl(document.id)}>
            下载原文件
          </a>
        </div>
      ) : (
        <div className={styles.placeholder}>
          <a
            className={styles.linkButton}
            href={document.source_url ?? '#'}
            target="_blank"
            rel="noreferrer"
          >
            打开原网页
          </a>
        </div>
      )}
    </div>
  )
}
