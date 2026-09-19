import { useEffect, useMemo, useRef, useState } from 'react'

import { ChatInput } from '@/components/chat/ChatInput'
import { ChatMessageList } from '@/components/chat/ChatMessageList'
import { ConversationList } from '@/components/chat/ConversationList'
import { BookIcon, CloseIcon, GlobeIcon, GlobeOffIcon, MessageCircleIcon } from '@/components/icons'
import { useChatStream } from '@/hooks/useChat'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { useConversations, useDeleteConversation } from '@/hooks/useConversations'
import { useKnowledgeBases } from '@/hooks/useKnowledgeBases'

import styles from './index.module.scss'

export default function Home() {
  // 检索范围：联网搜索开关 + @ 知识库多选，二者互斥
  const [webSearch, setWebSearch] = useState(true)
  const [selectedKbIds, setSelectedKbIds] = useState<string[]>([])
  const [kbPickerOpen, setKbPickerOpen] = useState(false)
  const kbIds = useMemo(() => (webSearch ? [] : selectedKbIds), [webSearch, selectedKbIds])
  const chat = useChatStream(null, kbIds, webSearch)
  const scrollRef = useAutoScroll([chat.messages, chat.streaming])
  const autoSelectedRef = useRef(false)
  const pickerRef = useRef<HTMLDivElement>(null)
  const kbButtonRef = useRef<HTMLButtonElement>(null)
  const { data: conversationData } = useConversations(null)
  const deleteConversation = useDeleteConversation()
  const { data: kbData } = useKnowledgeBases()

  const kbs = useMemo(() => kbData?.pages.flatMap((page) => page.items) ?? [], [kbData])
  const selectedKbs = useMemo(
    () => kbs.filter((kb) => selectedKbIds.includes(kb.id)),
    [kbs, selectedKbIds],
  )

  // 首次加载时默认选中第一条历史会话，避免落地空白
  useEffect(() => {
    const items = conversationData?.items ?? []
    if (!autoSelectedRef.current && items.length > 0) {
      autoSelectedRef.current = true
      void chat.selectConversation(items[0].id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationData])

  // 点击选择器外部时收起 @ 下拉（@ 按钮自身除外，避免与切换逻辑冲突）
  useEffect(() => {
    if (!kbPickerOpen) return
    function handleClickOutside(event: MouseEvent) {
      const target = event.target as Node
      if (
        pickerRef.current &&
        !pickerRef.current.contains(target) &&
        kbButtonRef.current &&
        !kbButtonRef.current.contains(target)
      ) {
        setKbPickerOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [kbPickerOpen])

  // 联网搜索开关：开 = 清空 @ 库回到联网；关 = 进入知识库模式（不联网也不检索库时即为纯 LLM）
  function handleToggleWeb() {
    if (webSearch) {
      setWebSearch(false)
    } else {
      setWebSearch(true)
      setSelectedKbIds([])
      setKbPickerOpen(false)
    }
  }

  function handleToggleKb(kbId: string) {
    setSelectedKbIds((prev) =>
      prev.includes(kbId) ? prev.filter((id) => id !== kbId) : [...prev, kbId],
    )
    setWebSearch(false)
  }

  function handleRemoveKb(kbId: string) {
    const next = selectedKbIds.filter((id) => id !== kbId)
    setSelectedKbIds(next)
  }

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
              <p className={styles.emptyText}>联网搜索，或基于知识库提问</p>
            </div>
          ) : (
            <ChatMessageList messages={chat.messages} streaming={chat.streaming} />
          )}
          {chat.error ? <div className={styles.error}>{chat.error}</div> : null}
        </div>

        <div className={styles.inputArea}>
          <div className={styles.scopeBar}>
            <button
              type="button"
              className={webSearch ? `${styles.webToggle} ${styles.webToggleOn}` : styles.webToggle}
              onClick={handleToggleWeb}
              aria-pressed={webSearch}
              title="联网搜索"
            >
              {webSearch ? (
                <GlobeIcon className={styles.webToggleIcon} />
              ) : (
                <GlobeOffIcon className={styles.webToggleIcon} />
              )}
              联网搜索
            </button>

            <button
              type="button"
              ref={kbButtonRef}
              className={selectedKbIds.length > 0 ? `${styles.kbToggle} ${styles.kbToggleOn}` : styles.kbToggle}
              onClick={() => setKbPickerOpen((open) => !open)}
              title="基于知识库提问"
            >
              <BookIcon className={styles.kbToggleIcon} />
              基于知识库
            </button>

            {selectedKbs.map((kb) => (
              <span key={kb.id} className={styles.kbChip}>
                {kb.name}
                <button type="button" onClick={() => handleRemoveKb(kb.id)} aria-label="移除">
                  <CloseIcon />
                </button>
              </span>
            ))}
          </div>

          {kbPickerOpen ? (
            <div className={styles.picker} ref={pickerRef}>
              {kbs.map((kb) => {
                const active = selectedKbIds.includes(kb.id)
                return (
                  <button
                    key={kb.id}
                    type="button"
                    className={active ? `${styles.pickerItem} ${styles.pickerItemActive}` : styles.pickerItem}
                    onClick={() => handleToggleKb(kb.id)}
                  >
                    <span className={styles.pickerCheck}>{active ? '✓' : ''}</span>
                    {kb.name}
                  </button>
                )
              })}
              {kbs.length === 0 ? <div className={styles.pickerEmpty}>暂无知识库</div> : null}
            </div>
          ) : null}

          <ChatInput
            streaming={chat.streaming}
            placeholder="输入问题"
            onSend={(text) => void chat.send(text)}
            onStop={chat.stop}
          />
        </div>
      </section>
    </div>
  )
}
