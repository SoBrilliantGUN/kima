import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { useCreateNoteFromUrl } from '@/hooks/useNotes'
import styles from './index.module.scss'

interface WebNoteFormModalProps {
  onClose: () => void
  knowledgeBaseId?: string
}

export function WebNoteFormModal({ onClose, knowledgeBaseId }: WebNoteFormModalProps) {
  const [url, setUrl] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const createFromUrl = useCreateNoteFromUrl()
  const navigate = useNavigate()

  useEffect(() => {
    function handleKeydown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeydown)
    return () => window.removeEventListener('keydown', handleKeydown)
  }, [onClose])

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const note = await createFromUrl.mutateAsync({
        url: url.trim(),
        knowledge_base_id: knowledgeBaseId ?? null,
      })
      onClose()
      navigate(`/notes/${note.id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '采集失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={(event) => event.stopPropagation()}>
        <h3 className={styles.title}>新建网页笔记</h3>
        <form onSubmit={handleSubmit}>
          <label className={styles.field}>
            <span>网页链接</span>
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="粘贴 http/https 链接"
              required
              autoFocus
            />
          </label>
          {error ? <p className={styles.error}>{error}</p> : null}
          <div className={styles.actions}>
            <button type="button" onClick={onClose}>
              取消
            </button>
            <button type="submit" className={styles.primary} disabled={submitting}>
              {submitting ? '采集并保存…' : '确定'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
