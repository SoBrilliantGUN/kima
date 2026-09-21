import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { ChatInput } from '@/components/chat/ChatInput'
import type { ChatMessage } from '@/api/types'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { CopilotSteps } from '../CopilotSteps'
import { CopilotTrace } from '../CopilotTrace'

import styles from './index.module.scss'

interface Props {
  messages: ChatMessage[]
  streaming: boolean
  error: string | null
  onSend: (text: string) => void
  onStop: () => void
}

export function CopilotChat({ messages, streaming, error, onSend, onStop }: Props) {
  const scrollRef = useAutoScroll([messages, streaming])

  return (
    <div className={styles.chat}>
      <div className={styles.messages} ref={scrollRef}>
        {messages.length === 0 && !streaming ? (
          <div className={styles.empty}>
            <p className={styles.emptyText}>我能检索、读文档、联网、写笔记，还能记住你</p>
          </div>
        ) : (
          messages.map((message, index) => {
            const isLast = index === messages.length - 1
            if (message.role === 'user') {
              return (
                <div key={message.id} className={styles.userRow}>
                  <div className={styles.userBubble}>{message.content}</div>
                </div>
              )
            }
            return (
              <div key={message.id} className={styles.assistantRow}>
                {message.content ? (
                  <div className={styles.markdown}>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                  </div>
                ) : streaming ? (
                  <div className={styles.loading} aria-label="Copilot 正在思考">
                    <span />
                    <span />
                    <span />
                  </div>
                ) : null}
                {message.steps && message.steps.length > 0 ? (
                  isLast && streaming ? (
                    <CopilotSteps steps={message.steps} streaming={streaming} />
                  ) : (
                    <CopilotTrace steps={message.steps} />
                  )
                ) : null}
              </div>
            )
          })
        )}
        {error ? <div className={styles.error}>{error}</div> : null}
      </div>

      <div className={styles.inputArea}>
        <ChatInput
          streaming={streaming}
          placeholder="让 Copilot 帮你办事…"
          onSend={onSend}
          onStop={onStop}
        />
      </div>
    </div>
  )
}
