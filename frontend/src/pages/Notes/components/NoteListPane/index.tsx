import { useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import type { Note } from '@/api/types'
import { PlusIcon } from '@/components/icons'
import { useInView } from '@/hooks/useInView'
import { useCreateNote, useDeleteNote, useNotes } from '@/hooks/useNotes'
import { forgetNote } from '@/lib/lastNote'
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

  const { ref: loadMoreRef, inView } = useInView<HTMLLIElement>(LOAD_MORE_OPTIONS)

  const items = data?.pages.flatMap((page) => page.items) ?? []

  useEffect(() => {
    if (inView && hasNextPage && !isFetchingNextPage) {
      void fetchNextPage()
    }
  }, [inView, hasNextPage, isFetchingNextPage, fetchNextPage])

  async function handleCreateBlank() {
    const note = await createBlank.mutateAsync({})
    navigate(`/notes/${note.id}`)
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
        <button
          type="button"
          className={styles.newButton}
          aria-label="新建笔记"
          title="新建笔记"
          onClick={() => void handleCreateBlank()}
        >
          <PlusIcon />
        </button>
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
    </aside>
  )
}
