import { useEffect, useMemo, useRef, useState } from 'react'

import { ChatInput } from '@/components/chat/ChatInput'
import { ChatMessageList } from '@/components/chat/ChatMessageList'
import { ConversationList } from '@/components/chat/ConversationList'
import { CloseIcon, HistoryIcon, MessageCircleIcon, PlusIcon } from '@/components/icons'
import { useChatStream } from '@/hooks/useChat'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { useConversations, useDeleteConversation } from '@/hooks/useConversations'

import styles from './index.module.scss'

interface Props {
  kbId: string | undefined
  onClose: () => void
  collapsed?: boolean
}

export function QaPanel({ kbId, onClose, collapsed = false }: Props) {
  const kbIds = useMemo(() => (kbId ? [kbId] : []), [kbId])
  const chat = useChatStream(kbId ?? null, kbIds, false)
  const scrollRef = useAutoScroll([chat.messages, chat.streaming])
  const { data: conversationData } = useConversations(kbId ?? null)
  const deleteConversation = useDeleteConversation()
  const [historyOpen, setHistoryOpen] = useState(false)
  const autoLoadedKbRef = useRef<string | null>(null)

  // 切换知识库：清空旧会话，准备载入新库最近会话
  useEffect(() => {
    chat.startNew()
    autoLoadedKbRef.current = null
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId])

  // 进入知识库后自动载入该库最近一条会话，避免空白
  useEffect(() => {
    if (autoLoadedKbRef.current === (kbId ?? null)) return
    const items = conversationData?.items ?? []
    if (items.length > 0) {
      autoLoadedKbRef.current = kbId ?? null
      void chat.selectConversation(items[0].id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationData, kbId])

  function handleNew() {
    setHistoryOpen(false)
    chat.startNew()
  }

  function handleSelect(id: string) {
    setHistoryOpen(false)
    void chat.selectConversation(id)
  }

  return (
    <aside className={collapsed ? `${styles.panel} ${styles.collapsed}` : styles.panel}>
      <div className={styles.top}>
        <div className={styles.header}>
          <button
            type="button"
            className={styles.iconButton}
            onClick={handleNew}
            aria-label="新建对话"
            title="新建对话"
          >
            <PlusIcon />
          </button>
          <button
            type="button"
            className={styles.iconButton}
            onClick={() => setHistoryOpen((open) => !open)}
            aria-label="历史对话"
            title="历史对话"
          >
            <HistoryIcon />
          </button>
          <button
            type="button"
            className={styles.iconButton}
            onClick={onClose}
            aria-label="收起问答"
            title="收起问答"
          >
            <CloseIcon />
          </button>
        </div>

        {historyOpen ? (
          <div className={styles.dropdown}>
            <ConversationList
              hideNewButton
              conversations={conversationData?.items ?? []}
              activeId={chat.conversationId}
              onSelect={handleSelect}
              onDelete={(id) => void deleteConversation.mutate(id)}
            />
          </div>
        ) : null}
      </div>

      <div className={styles.messages} ref={scrollRef}>
        {chat.messages.length === 0 && !chat.streaming ? (
          <div className={styles.empty}>
            <MessageCircleIcon className={styles.emptyIcon} />
            <p className={styles.emptyText}>基于知识库问答</p>
          </div>
        ) : (
          <ChatMessageList messages={chat.messages} streaming={chat.streaming} />
        )}
        {chat.error ? <div className={styles.error}>{chat.error}</div> : null}
      </div>

      <div className={styles.inputArea}>
        <ChatInput
          streaming={chat.streaming}
          placeholder="基于知识库提问"
          onSend={(text) => void chat.send(text)}
          onStop={chat.stop}
        />
      </div>
    </aside>
  )
}
