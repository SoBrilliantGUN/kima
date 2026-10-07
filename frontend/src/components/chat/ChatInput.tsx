import { useLayoutEffect, useRef, useState } from 'react'
import type { ChangeEvent, KeyboardEvent, PointerEvent } from 'react'

import { SendIcon, StopIcon } from '@/components/icons'

import styles from './ChatInput.module.scss'

const MIN_HEIGHT = 40
const MAX_HEIGHT = 240
const STORAGE_KEY = 'kima:chat-input-height'

function loadHeight(): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const n = Number(raw)
    return Number.isFinite(n) ? Math.min(Math.max(n, MIN_HEIGHT), MAX_HEIGHT) : null
  } catch {
    return null
  }
}

interface Props {
  streaming: boolean
  placeholder: string
  onSend: (text: string) => void
  onStop: () => void
}

export function ChatInput({ streaming, placeholder, onSend, onStop }: Props) {
  const [value, setValue] = useState('')
  // 用户手动拖拽的高度（null = 自动撑高；有值 = 尊重手动高度，持久化到 localStorage）
  const [height, setHeight] = useState<number | null>(loadHeight)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const dragRef = useRef<{ startY: number; startHeight: number } | null>(null)
  const hasText = value.trim().length > 0
  const active = streaming || hasText

  useLayoutEffect(() => {
    if (height !== null) return
    const el = textareaRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`
    }
  }, [value, height])

  function handleChange(event: ChangeEvent<HTMLTextAreaElement>) {
    setValue(event.target.value)
  }

  function handleSend() {
    if (!hasText) return
    onSend(value)
    setValue('')
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      handleSend()
    }
  }

  function handlePointerDown(event: PointerEvent<HTMLDivElement>) {
    const el = textareaRef.current
    if (!el) return
    dragRef.current = { startY: event.clientY, startHeight: el.offsetHeight }
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!dragRef.current) return
    // 向上拉（clientY 减小）→ 高度增大
    const delta = dragRef.current.startY - event.clientY
    const next = Math.min(Math.max(dragRef.current.startHeight + delta, MIN_HEIGHT), MAX_HEIGHT)
    setHeight(next)
    try {
      localStorage.setItem(STORAGE_KEY, String(next))
    } catch {
      // 隐私模式等场景写失败，忽略
    }
  }

  function handlePointerUp() {
    dragRef.current = null
  }

  return (
    <div className={styles.inputBox}>
      <div
        className={styles.dragHandle}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        aria-hidden
      />
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={2}
        style={height !== null ? { height } : undefined}
      />
      <button
        type="button"
        className={active ? `${styles.send} ${styles.sendActive}` : `${styles.send} ${styles.sendDisabled}`}
        onClick={streaming ? onStop : handleSend}
        disabled={!active}
        aria-label={streaming ? '停止' : '发送'}
      >
        {streaming ? <StopIcon className={styles.sendIcon} /> : <SendIcon className={styles.sendIcon} />}
      </button>
    </div>
  )
}
