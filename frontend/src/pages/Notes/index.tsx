import { useEffect } from 'react'
import { useParams } from 'react-router-dom'

import { rememberNote } from '@/lib/lastNote'
import { NoteEditorPane } from './components/NoteEditorPane'
import { NoteListPane } from './components/NoteListPane'
import styles from './index.module.scss'

export default function Notes() {
  const { id } = useParams<{ id: string }>()

  useEffect(() => {
    if (id) rememberNote(id)
  }, [id])

  return (
    <div className={styles.page}>
      <NoteListPane selectedId={id} />
      {id ? (
        <NoteEditorPane key={id} noteId={id} />
      ) : (
        <div className={styles.empty}>从左侧选择或新建一条笔记</div>
      )}
    </div>
  )
}
