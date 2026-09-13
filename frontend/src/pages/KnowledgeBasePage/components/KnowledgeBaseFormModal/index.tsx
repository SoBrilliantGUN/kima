import { useEffect, useState, type FormEvent } from 'react'

import { ApiError } from '@/api/client'
import type { KnowledgeBase, KnowledgeBaseCreate } from '@/api/types'
import styles from './index.module.scss'

const COLOR_PRESETS = ['#5B8DEF', '#EF6C6C', '#F5A623', '#4BB543', '#9B59B6', '#34495E']

interface KnowledgeBaseFormModalProps {
  initial: KnowledgeBase | null
  onClose: () => void
  onSubmit: (values: KnowledgeBaseCreate) => Promise<unknown>
}

export function KnowledgeBaseFormModal({
  initial,
  onClose,
  onSubmit,
}: KnowledgeBaseFormModalProps) {
  const [name, setName] = useState(initial?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [color, setColor] = useState(initial?.color ?? COLOR_PRESETS[0])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
      await onSubmit({
        name: name.trim(),
        description: description.trim() || null,
        color,
      })
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '提交失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={(event) => event.stopPropagation()}>
        <h3 className={styles.title}>{initial ? '编辑知识库' : '新建知识库'}</h3>
        <form onSubmit={handleSubmit}>
          <label className={styles.field}>
            <span>名称</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              maxLength={255}
              autoFocus
            />
          </label>
          <label className={styles.field}>
            <span>描述</span>
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          <div className={styles.field}>
            <span>颜色</span>
            <div className={styles.colors}>
              {COLOR_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className={color === preset ? styles.colorActive : styles.color}
                  style={{ backgroundColor: preset }}
                  onClick={() => setColor(preset)}
                  aria-label={preset}
                />
              ))}
            </div>
          </div>
          {error ? <p className={styles.error}>{error}</p> : null}
          <div className={styles.actions}>
            <button type="button" onClick={onClose}>
              取消
            </button>
            <button type="submit" className={styles.primary} disabled={submitting}>
              {submitting ? '保存中…' : '保存'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
