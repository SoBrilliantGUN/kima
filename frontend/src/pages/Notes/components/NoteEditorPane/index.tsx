import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Image from '@tiptap/extension-image'
import Placeholder from '@tiptap/extension-placeholder'
import { EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import { Markdown } from 'tiptap-markdown'

import { useDeleteNote, useNote, useUpdateNote } from '@/hooks/useNotes'
import { forgetNote } from '@/lib/lastNote'
import { AddToKnowledgeBaseModal } from '@/pages/Notes/components/AddToKnowledgeBaseModal'
import { NoteEditorToolbar } from '@/pages/Notes/components/NoteEditorToolbar'
import styles from './index.module.scss'

interface NoteEditorPaneProps {
  noteId: string
}

// 正文自动保存的防抖延迟（毫秒）
const AUTOSAVE_DELAY = 800

// 笔记编辑区：标题 + 正文（Tiptap 富文本）+ 元信息/操作
export function NoteEditorPane({ noteId }: NoteEditorPaneProps) {
  const { data: note, isLoading, isError } = useNote(noteId)
  const updateMutation = useUpdateNote()
  const deleteMutation = useDeleteNote()
  const navigate = useNavigate()

  const [title, setTitle] = useState('')
  const [addKbOpen, setAddKbOpen] = useState(false)
  // 是否已从服务端数据回填过一次，避免后续重取数据时覆盖用户编辑
  const initialized = useRef(false)
  // 正文自动保存的定时器句柄
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 尚未触发保存的最新正文内容，用于卸载时兜底 flush
  const pendingMarkdown = useRef<string | null>(null)

  const editor = useEditor({
    extensions: [
      StarterKit,
      Image,
      Placeholder.configure({ placeholder: '开始输入正文…' }),
      Markdown.configure({ html: false }),
    ],
    content: '',
    immediatelyRender: false,
    // 每次编辑后以 Markdown 形式防抖保存正文
    onUpdate: ({ editor: current }) => {
      const markdown = current.storage.markdown.getMarkdown()
      // 先暂存内容，供卸载时兜底 flush 使用
      pendingMarkdown.current = markdown
      if (saveTimer.current) clearTimeout(saveTimer.current)
      saveTimer.current = setTimeout(() => {
        // 保存已发出，清空暂存内容
        pendingMarkdown.current = null
        void updateMutation.mutateAsync({
          id: noteId,
          payload: { content_markdown: markdown },
        })
      }, AUTOSAVE_DELAY)
    },
  })

  // 首次加载回填标题与正文（仅一次，避免重取数据时重置编辑状态）
  useEffect(() => {
    if (note && editor && !initialized.current) {
      setTitle(note.title)
      editor.commands.setContent(note.content_markdown, false)
      initialized.current = true
    }
  }, [note, editor])

  // 卸载前 flush 未保存的正文
  useEffect(() => {
    return () => {
      if (saveTimer.current) {
        clearTimeout(saveTimer.current)
        if (pendingMarkdown.current !== null) {
          void updateMutation.mutateAsync({
            id: noteId,
            payload: { content_markdown: pendingMarkdown.current },
          })
        }
      }
    }
    // noteId 由 key 固定，本 effect 仅在卸载时清理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function handleTitleBlur() {
    if (!note) return
    const trimmed = title.trim()
    // 标题有改动才保存；被清空时回退为原标题（不允许空标题）
    if (trimmed && trimmed !== note.title) {
      void updateMutation.mutateAsync({ id: noteId, payload: { title: trimmed } })
    } else if (!trimmed) {
      setTitle(note.title)
    }
  }

  async function handleDelete() {
    if (!window.confirm(`确定删除笔记「${note?.title ?? ''}」吗？该操作不可恢复。`)) return
    await deleteMutation.mutateAsync(noteId)
    // 删除后清除「上次打开的笔记」记录并返回列表页
    forgetNote()
    navigate('/notes')
  }

  if (isLoading) {
    return <section className={styles.pane}>加载中…</section>
  }
  if (isError || !note) {
    return <section className={styles.pane}>笔记不存在或已删除。</section>
  }

  return (
    <section className={styles.pane}>
      <NoteEditorToolbar editor={editor} />

      <div className={styles.scroll}>
        <div className={styles.content}>
          <input
            className={styles.title}
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            onBlur={handleTitleBlur}
            placeholder="无标题笔记"
          />

          <EditorContent editor={editor} className={styles.editor} />
        </div>
      </div>

      <footer className={styles.footer}>
        <button type="button" onClick={() => setAddKbOpen(true)}>
          添加到知识库
        </button>
        <button type="button" onClick={() => void handleDelete()}>
          删除
        </button>
      </footer>

      {/* 按需渲染「添加到知识库」弹窗 */}
      {addKbOpen ? (
        <AddToKnowledgeBaseModal noteId={noteId} onClose={() => setAddKbOpen(false)} />
      ) : null}
    </section>
  )
}
