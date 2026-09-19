import { useEffect, useMemo, useRef, useState } from 'react'

import { ChatInput } from '@/components/chat/ChatInput'
import { ChatMessageList } from '@/components/chat/ChatMessageList'
import { ConversationList } from '@/components/chat/ConversationList'
import { BookIcon, GlobeIcon, MessageCircleIcon } from '@/components/icons'
import { useChatStream } from '@/hooks/useChat'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { useConversations, useDeleteConversation } from '@/hooks/useConversations'
import { useKnowledgeBases } from '@/hooks/useKnowledgeBases'
import type { ChatMode } from '@/api/types'

import styles from './index.module.scss'

export default function Home() {
  const [mode, setMode] = useState<ChatMode>('web')
  const [kbId, setKbId] = useState<string | null>(null)
  const chat = useChatStream(mode, kbId)
  const scrollRef = useAutoScroll([chat.messages, chat.streaming])
  const autoSelectedRef = useRef(false)
  const { data: conversationData } = useConversations(null)
  const deleteConversation = useDeleteConversation()
  const { data: kbData } = useKnowledgeBases()

  const kbs = useMemo(() => kbData?.pages.flatMap((page) => page.items) ?? [], [kbData])

  // 切换模式 / 库时开新会话
  useEffect(() => {
    chat.startNew()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, kbId])

  // 首次加载时默认选中第一条历史会话，避免落地空白
  useEffect(() => {
    const items = conversationData?.items ?? []
    if (!autoSelectedRef.current && items.length > 0) {
      autoSelectedRef.current = true
      void chat.selectConversation(items[0].id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationData])

  const needsKb = mode === 'kb' && !kbId

  return (
    <div className={styles.page}>
      <ConversationList
        className={styles.sidebar}
        conversations={conversationData?.items ?? []}
        activeId={chat.conversationId}
        onSelect={(id) => void chat.selectConversation(id)}
        onNew={() => chat.startNew()}
        onDelete={(id) => void deleteConversation.mutate(id)}
      />
      <section className={styles.chat}>
        <div className={styles.messages} ref={scrollRef}>
          {chat.messages.length === 0 && !chat.streaming ? (
            <div className={styles.empty}>
              <MessageCircleIcon className={styles.emptyIcon} />
              <p className={styles.emptyText}>
                {mode === 'web' ? '基于全网问答' : '基于知识库问答'}
              </p>
            </div>
          ) : (
            <ChatMessageList messages={chat.messages} streaming={chat.streaming} />
          )}
          {chat.error ? <div className={styles.error}>{chat.error}</div> : null}
        </div>

        <div className={styles.inputArea}>
          <div className={styles.modeSwitch}>
            <button
              type="button"
              className={mode === 'web' ? `${styles.mode} ${styles.modeActive}` : styles.mode}
              onClick={() => setMode('web')}
            >
              <GlobeIcon className={styles.modeIcon} />
              基于全网
            </button>
            <button
              type="button"
              className={mode === 'kb' ? `${styles.mode} ${styles.modeActive}` : styles.mode}
              onClick={() => setMode('kb')}
            >
              <BookIcon className={styles.modeIcon} />
              基于知识库
            </button>
          </div>
          {mode === 'kb' ? (
            <select
              className={styles.kbSelect}
              value={kbId ?? ''}
              onChange={(event) => setKbId(event.target.value || null)}
            >
              <option value="">选择知识库</option>
              {kbs.map((kb) => (
                <option key={kb.id} value={kb.id}>
                  {kb.name}
                </option>
              ))}
            </select>
          ) : null}
          {needsKb ? (
            <div className={styles.selectHint}>请先选择知识库</div>
          ) : (
            <ChatInput
              streaming={chat.streaming}
              placeholder={mode === 'web' ? '基于全网提问' : '基于知识库提问'}
              onSend={(text) => void chat.send(text)}
              onStop={chat.stop}
            />
          )}
        </div>
      </section>
    </div>
  )
}
