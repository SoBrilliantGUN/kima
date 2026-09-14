import type { Editor } from '@tiptap/react'

import styles from './index.module.scss'

interface NoteEditorToolbarProps {
  editor: Editor | null
}

interface ToolButton {
  label: string
  title: string
  active?: () => boolean
  disabled?: () => boolean
  run: () => void
}

export function NoteEditorToolbar({ editor }: NoteEditorToolbarProps) {
  if (!editor) return null

  const buttons: ToolButton[] = [
    {
      label: '撤销',
      title: '撤销',
      disabled: () => !editor.can().undo(),
      run: () => editor.chain().focus().undo().run(),
    },
    {
      label: '重做',
      title: '重做',
      disabled: () => !editor.can().redo(),
      run: () => editor.chain().focus().redo().run(),
    },
    { label: '加粗', title: '加粗', active: () => editor.isActive('bold'), run: () => editor.chain().focus().toggleBold().run() },
    { label: '斜体', title: '斜体', active: () => editor.isActive('italic'), run: () => editor.chain().focus().toggleItalic().run() },
    { label: '删除线', title: '删除线', active: () => editor.isActive('strike'), run: () => editor.chain().focus().toggleStrike().run() },
    { label: 'H1', title: '一级标题', active: () => editor.isActive('heading', { level: 1 }), run: () => editor.chain().focus().toggleHeading({ level: 1 }).run() },
    { label: 'H2', title: '二级标题', active: () => editor.isActive('heading', { level: 2 }), run: () => editor.chain().focus().toggleHeading({ level: 2 }).run() },
    { label: 'H3', title: '三级标题', active: () => editor.isActive('heading', { level: 3 }), run: () => editor.chain().focus().toggleHeading({ level: 3 }).run() },
    { label: '无序', title: '无序列表', active: () => editor.isActive('bulletList'), run: () => editor.chain().focus().toggleBulletList().run() },
    { label: '有序', title: '有序列表', active: () => editor.isActive('orderedList'), run: () => editor.chain().focus().toggleOrderedList().run() },
    { label: '引用', title: '引用', active: () => editor.isActive('blockquote'), run: () => editor.chain().focus().toggleBlockquote().run() },
    { label: '代码块', title: '代码块', active: () => editor.isActive('codeBlock'), run: () => editor.chain().focus().toggleCodeBlock().run() },
    { label: '行内代码', title: '行内代码', active: () => editor.isActive('code'), run: () => editor.chain().focus().toggleCode().run() },
  ]

  return (
    <div className={styles.toolbar}>
      {buttons.map((button) => {
        const active = button.active?.() ?? false
        const disabled = button.disabled?.() ?? false
        return (
          <button
            key={button.label}
            type="button"
            title={button.title}
            className={active ? `${styles.button} ${styles.active}` : styles.button}
            disabled={disabled}
            onClick={button.run}
          >
            {button.label}
          </button>
        )
      })}
    </div>
  )
}
