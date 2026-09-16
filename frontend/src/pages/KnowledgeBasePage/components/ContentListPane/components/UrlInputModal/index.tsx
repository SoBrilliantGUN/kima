import { useEffect, useRef, useState, type FormEvent } from 'react'

import { ApiError } from '@/api/client'
import { CloseIcon } from '@/components/icons'
import { Modal } from '@/components/Modal'
import { useCreateDocumentFromUrl } from '@/hooks/useDocuments'
import styles from './index.module.scss'

interface UrlInputModalProps {
  kbId: string
  onClose: () => void
}

export function UrlInputModal({ kbId, onClose }: UrlInputModalProps) {
  const [url, setUrl] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const createFromUrl = useCreateDocumentFromUrl()
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const trimmed = url.trim()
    if (!trimmed) {
      setError('请输入链接')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await createFromUrl.mutateAsync({ url: trimmed, knowledge_base_id: kbId })
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败，请重试')
      setSubmitting(false)
    }
  }

  return (
    <Modal onClose={onClose} className={styles.modal}>
      <header className={styles.header}>
        <h3 className={styles.title}>添加 URL 文档</h3>
        <button type="button" className={styles.close} onClick={onClose} aria-label="关闭">
          <CloseIcon />
        </button>
      </header>
      <form onSubmit={handleSubmit}>
        <label className={styles.field}>
          <span>网页链接</span>
          <input
            ref={inputRef}
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="粘贴 http/https 链接"
          />
        </label>
        {error ? <p className={styles.error}>{error}</p> : null}
        <div className={styles.actions}>
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className={styles.primary} disabled={submitting}>
            {submitting ? '提交中…' : '确定'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
