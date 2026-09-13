import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from 'react'

import { ChevronDownIcon, MessageCircleIcon, SendIcon, StopIcon } from '@/components/icons'
import styles from './index.module.scss'

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
}

function copyText(text: string) {
  void navigator.clipboard.writeText(text)
}

function UserMessage({ content }: { content: string }) {
  return (
    <div className={styles.userRow}>
      <div className={styles.userBubble}>{content}</div>
      <div className={styles.userActions}>
        <button type="button">修改</button>
        <button type="button" onClick={() => copyText(content)}>
          复制
        </button>
      </div>
    </div>
  )
}

function AssistantMessage({ content }: { content: string }) {
  return (
    <div className={styles.assistantRow}>
      <div className={styles.assistantContent}>{content}</div>
      <div className={styles.assistantActions}>
        <button type="button" onClick={() => copyText(content)}>
          复制
        </button>
        <button type="button">记笔记</button>
        <button type="button">删除</button>
      </div>
    </div>
  )
}

export function QaPanel() {
  // 本地占位：模块 5 接入后由后端流式写入真实消息
  const [messages] = useState<Message[]>([])
  const [value, setValue] = useState('')
  const [generating, setGenerating] = useState(false)
  const [showScrollButton, setShowScrollButton] = useState(false)
  const messagesRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const hasText = value.trim().length > 0
  const active = generating || hasText

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
    setGenerating(true)
  }

  function handleStop() {
    setGenerating(false)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      handleSend()
    }
  }

  const updateScrollButton = useCallback(() => {
    const el = messagesRef.current
    if (!el) return
    setShowScrollButton(el.scrollHeight > el.clientHeight)
  }, [])

  // 内容溢出（文本过长）时才显示按钮，与滚动位置无关；内容高度变化时重算
  useEffect(() => {
    const el = messagesRef.current
    if (!el) return
    updateScrollButton()
    const observer = new ResizeObserver(updateScrollButton)
    observer.observe(el)
    return () => observer.disconnect()
  }, [updateScrollButton])

  function scrollToBottom() {
    const el = messagesRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  return (
    <aside className={styles.panel}>
      <div className={styles.messages} ref={messagesRef}>
        {messages.length === 0 ? (
          <div className={styles.empty}>
            <MessageCircleIcon className={styles.emptyIcon} />
            <p className={styles.emptyText}>基于知识库问答</p>
          </div>
        ) : (
          messages.map((message) =>
            message.role === 'user' ? (
              <UserMessage key={message.id} content={message.content} />
            ) : (
              <AssistantMessage key={message.id} content={message.content} />
            ),
          )
        )}
      </div>

      <div className={styles.inputArea}>
        <div className={styles.fade}>
          {showScrollButton ? (
            <button
              type="button"
              className={styles.scrollButton}
              onClick={scrollToBottom}
              aria-label="滚动到底部"
            >
              <ChevronDownIcon />
            </button>
          ) : null}
        </div>
        <div className={styles.inputBox}>
          <textarea
            ref={textareaRef}
            value={value}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder="基于知识库提问"
            rows={2}
          />
          <button
            type="button"
            className={
              active ? `${styles.send} ${styles.sendActive}` : `${styles.send} ${styles.sendDisabled}`
            }
            onClick={generating ? handleStop : handleSend}
            disabled={!active}
            aria-label={generating ? '停止' : '发送'}
          >
            {generating ? (
              <StopIcon className={styles.sendIcon} />
            ) : (
              <SendIcon className={styles.sendIcon} />
            )}
          </button>
        </div>
      </div>
    </aside>
  )
}
