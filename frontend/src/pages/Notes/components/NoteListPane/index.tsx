import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import type { Note } from '@/api/types'
import { useInView } from '@/hooks/useInView'
import { useCreateNote, useDeleteNote, useNotes } from '@/hooks/useNotes'
import { forgetNote } from '@/lib/lastNote'
import { WebNoteFormModal } from '@/pages/Notes/components/WebNoteFormModal'
import styles from './index.module.scss'

interface NoteListPaneProps {
  selectedId?: string
}

// 提前 200px 触发加载，避免用户滚到底才等待
const LOAD_MORE_OPTIONS: IntersectionObserverInit = { rootMargin: '200px' }

export function NoteListPane({ selectedId }: NoteListPaneProps) {
  const { data, isLoading, isError, hasNextPage, isFetchingNextPage, fetchNextPage } = useNotes()
  const createBlank = useCreateNote()
  const deleteMutation = useDeleteNote()
  const navigate = useNavigate()

  const [menuOpen, setMenuOpen] = useState(false)
  const [webModalOpen, setWebModalOpen] = useState(false)
  const newWrapRef = useRef<HTMLDivElement>(null)

  const { ref: loadMoreRef, inView } = useInView<HTMLLIElement>(LOAD_MORE_OPTIONS)

  const items = data?.pages.flatMap((page) => page.items) ?? []

  useEffect(() => {
    if (inView && hasNextPage && !isFetchingNextPage) {
      void fetchNextPage()
    }
  }, [inView, hasNextPage, isFetchingNextPage, fetchNextPage])

  // 点击「新建」下拉以外的区域时收起菜单
  useEffect(() => {
    if (!menuOpen) return
    function handleMouseDown(event: MouseEvent) {
      if (newWrapRef.current && !newWrapRef.current.contains(event.target as Node)) {
        setMenuOpen(false)
      }
    }
    window.addEventListener('mousedown', handleMouseDown)
    return () => window.removeEventListener('mousedown', handleMouseDown)
  }, [menuOpen])

  async function handleCreateBlank() {
    setMenuOpen(false)
    const note = await createBlank.mutateAsync({})
    navigate(`/notes/${note.id}`)
  }

  function handleOpenWeb() {
    setMenuOpen(false)
    setWebModalOpen(true)
  }

  async function handleDelete(note: Note) {
    if (!window.confirm(`确定删除笔记「${note.title}」吗？该操作不可恢复。`)) return
    await deleteMutation.mutateAsync(note.id)
    if (selectedId === note.id) {
      forgetNote()
      navigate('/notes')
    }
  }

  return (
    <aside className={styles.pane}>
      <header className={styles.header}>
        <h2 className={styles.title}>笔记</h2>
        <div className={styles.newWrap} ref={newWrapRef}>
          <button
            type="button"
            className={styles.newButton}
            onClick={() => setMenuOpen((open) => !open)}
          >
            新建
          </button>
          {menuOpen ? (
            <div className={styles.menu}>
              <button type="button" onClick={handleOpenWeb}>
                网页
              </button>
              <button type="button" onClick={() => void handleCreateBlank()}>
                空白笔记
              </button>
            </div>
          ) : null}
        </div>
      </header>

      {isLoading ? (
        <p className={styles.hint}>加载中…</p>
      ) : isError ? (
        <p className={styles.hint}>加载失败，请检查后端服务。</p>
      ) : items.length === 0 ? (
        <p className={styles.hint}>还没有笔记，点「新建」开始。</p>
      ) : (
        <ul className={styles.list}>
          {items.map((note) => (
            <li key={note.id} className={styles.item}>
              <Link
                to={`/notes/${note.id}`}
                className={
                  note.id === selectedId
                    ? `${styles.itemLink} ${styles.active}`
                    : styles.itemLink
                }
              >
                <span className={styles.itemTitle}>{note.title}</span>
                <span className={styles.itemSummary}>{note.summary || ' '}</span>
              </Link>
              <button
                type="button"
                className={styles.itemDelete}
                onClick={() => void handleDelete(note)}
              >
                删除
              </button>
            </li>
          ))}
          {hasNextPage ? (
            <li ref={loadMoreRef} className={styles.sentinel}>
              {isFetchingNextPage ? '加载中…' : null}
            </li>
          ) : null}
        </ul>
      )}

      {webModalOpen ? <WebNoteFormModal onClose={() => setWebModalOpen(false)} /> : null}
    </aside>
  )
}
