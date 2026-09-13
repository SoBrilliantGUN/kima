import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import type { KnowledgeBase, KnowledgeBaseCreate } from '@/api/types'
import {
  useCreateKnowledgeBase,
  useDeleteKnowledgeBase,
  useKnowledgeBases,
  useUpdateKnowledgeBase,
} from '@/hooks/useKnowledgeBases'
import { KnowledgeBaseFormModal } from '@/pages/KnowledgeBasePage/components/KnowledgeBaseFormModal'
import styles from './index.module.scss'

interface KnowledgeBaseListPaneProps {
  selectedId?: string
}

export function KnowledgeBaseListPane({ selectedId }: KnowledgeBaseListPaneProps) {
  const { data, isLoading, isError, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useKnowledgeBases()
  const createMutation = useCreateKnowledgeBase()
  const updateMutation = useUpdateKnowledgeBase()
  const deleteMutation = useDeleteKnowledgeBase()
  const navigate = useNavigate()

  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<KnowledgeBase | null>(null)

  const items = data?.pages.flatMap((page) => page.items) ?? []
  const total = data?.pages[0]?.total ?? 0
  const canDelete = total > 1

  function openCreate() {
    setEditing(null)
    setModalOpen(true)
  }

  function openEdit(kb: KnowledgeBase) {
    setEditing(kb)
    setModalOpen(true)
  }

  function handleSubmit(values: KnowledgeBaseCreate) {
    if (editing) {
      return updateMutation.mutateAsync({ id: editing.id, payload: values })
    }
    return createMutation.mutateAsync(values)
  }

  async function handleDelete(kb: KnowledgeBase) {
    if (!window.confirm(`确定删除知识库「${kb.name}」吗？该操作不可恢复。`)) return
    await deleteMutation.mutateAsync(kb.id)
    if (selectedId === kb.id) {
      const next = items.filter((item) => item.id !== kb.id)[0]
      if (next) navigate(`/knowledge-bases/${next.id}`)
    }
  }

  return (
    <aside className={styles.pane}>
      <header className={styles.header}>
        <h2 className={styles.title}>知识库</h2>
        <button type="button" className={styles.newButton} onClick={openCreate}>
          新建
        </button>
      </header>

      {isLoading ? (
        <p className={styles.hint}>加载中…</p>
      ) : isError ? (
        <p className={styles.hint}>加载失败，请检查后端服务。</p>
      ) : (
        <ul className={styles.list}>
          {items.map((kb) => (
            <li key={kb.id} className={styles.item}>
              <Link
                to={`/knowledge-bases/${kb.id}`}
                className={
                  kb.id === selectedId
                    ? `${styles.itemLink} ${styles.active}`
                    : styles.itemLink
                }
              >
                <span className={styles.cover} style={{ backgroundColor: kb.color }}>
                  {kb.name.charAt(0)}
                </span>
                <span className={styles.body}>
                  <span className={styles.name}>{kb.name}</span>
                  {kb.description ? (
                    <span className={styles.description}>{kb.description}</span>
                  ) : null}
                </span>
              </Link>
              <div className={styles.itemActions}>
                <button type="button" onClick={() => openEdit(kb)}>
                  编辑
                </button>
                {canDelete ? (
                  <button type="button" onClick={() => void handleDelete(kb)}>
                    删除
                  </button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}

      {hasNextPage ? (
        <button
          type="button"
          className={styles.loadMore}
          onClick={() => void fetchNextPage()}
          disabled={isFetchingNextPage}
        >
          {isFetchingNextPage ? '加载中…' : '加载更多'}
        </button>
      ) : null}

      {modalOpen ? (
        <KnowledgeBaseFormModal
          initial={editing}
          onClose={() => setModalOpen(false)}
          onSubmit={handleSubmit}
        />
      ) : null}
    </aside>
  )
}
