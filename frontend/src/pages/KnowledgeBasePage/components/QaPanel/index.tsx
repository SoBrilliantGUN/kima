import { useEffect } from 'react'

import { ChatInput } from '@/components/chat/ChatInput'
import { ChatMessageList } from '@/components/chat/ChatMessageList'
import { MessageCircleIcon } from '@/components/icons'
import { useChatStream } from '@/hooks/useChat'

import styles from './index.module.scss'

interface Props {
  kbId: string | undefined
}

export function QaPanel({ kbId }: Props) {
  const chat = useChatStream('kb', kbId ?? null)

  // 切换知识库时开新会话（会话按库持久化由后端 kb_id 保证）
  useEffect(() => {
    chat.startNew()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbId])

  return (
    <aside className={styles.panel}>
      <div className={styles.messages}>
        {chat.messages.length === 0 && !chat.streaming ? (
          <div className={styles.empty}>
            <MessageCircleIcon className={styles.emptyIcon} />
            <p className={styles.emptyText}>基于知识库问答</p>
          </div>
        ) : (
          <ChatMessageList messages={chat.messages} />
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
