import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { Note } from '@/api/types'
import { FilePlusIcon, SearchIcon } from '@/components/icons'
import { useKnowledgeBase, useKbContents } from '@/hooks/useKnowledgeBases'
import { useCreateNote } from '@/hooks/useNotes'
import { AddContentMenu } from '@/pages/KnowledgeBasePage/components/AddContentMenu'
import { WebNoteFormModal } from '@/pages/Notes/components/WebNoteFormModal'
import styles from './index.module.scss'

interface ContentListPaneProps {
  kbId?: string
}

export function ContentListPane({ kbId }: ContentListPaneProps) {
  const { data: kb, isLoading, isError } = useKnowledgeBase(kbId)
  const { data: contents } = useKbContents(kbId)
  const createNote = useCreateNote()
  const navigate = useNavigate()

  const [menuOpen, setMenuOpen] = useState(false)
  const [webModalOpen, setWebModalOpen] = useState(false)

  const notes = (contents?.items ?? [])
    .map((item) => item.note)
    .filter((note): note is Note => note !== null)

  async function handleSelect(type: 'web' | 'note') {
    setMenuOpen(false)
    if (!kbId) return
    if (type === 'note') {
      const note = await createNote.mutateAsync({ knowledge_base_id: kbId })
      navigate(`/notes/${note.id}`)
    } else {
      setWebModalOpen(true)
    }
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

      {notes.length === 0 ? (
        <div className={styles.empty}>
          <p>知识库里什么也没有</p>
          <p className={styles.emptyHint}>点「添加内容」新建笔记或采集网页</p>
        </div>
      ) : (
        <ul className={styles.list}>
          {notes.map((note) => (
            <li key={note.id}>
              <button
                type="button"
                className={styles.item}
                onClick={() => navigate(`/notes/${note.id}`)}
              >
                <span className={styles.itemTitle}>{note.title}</span>
                {note.summary ? <span className={styles.itemSummary}>{note.summary}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      )}

      {webModalOpen && kbId ? (
        <WebNoteFormModal onClose={() => setWebModalOpen(false)} knowledgeBaseId={kbId} />
      ) : null}
    </section>
  )
}
