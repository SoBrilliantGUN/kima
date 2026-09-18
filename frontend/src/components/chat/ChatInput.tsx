import { useLayoutEffect, useRef, useState } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'

import { SendIcon, StopIcon } from '@/components/icons'

import styles from './ChatInput.module.scss'

interface Props {
  streaming: boolean
  placeholder: string
  onSend: (text: string) => void
  onStop: () => void
}

export function ChatInput({ streaming, placeholder, onSend, onStop }: Props) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const hasText = value.trim().length > 0
  const active = streaming || hasText

  useLayoutEffect(() => {
    const el = textareaRef.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${el.scrollHeight}px`
    }
  }, [value])

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

  return (
    <div className={styles.inputBox}>
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        rows={2}
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
