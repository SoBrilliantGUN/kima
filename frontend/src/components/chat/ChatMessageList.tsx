import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import type { ChatMessage, Citation } from '@/api/types'

import styles from './ChatMessageList.module.scss'

function CitationPanel({ citations }: { citations: Citation[] }) {
  return (
    <div className={styles.citations}>
      <div className={styles.citationsHeader}>来源</div>
      {citations.map((citation) => (
        <div key={citation.index} className={styles.citationItem}>
          <span className={styles.citationIndex}>[{citation.index}]</span>
          <div className={styles.citationBody}>
            <div className={styles.citationTitle}>
              {citation.url ? (
                <a href={citation.url} target="_blank" rel="noreferrer">
                  {citation.title}
                </a>
              ) : (
                citation.title
              )}
            </div>
            <div className={styles.citationSnippet}>{citation.snippet}</div>
          </div>
        </div>
      ))}
    </div>
  )
}

export function ChatMessageList({ messages, streaming }: { messages: ChatMessage[]; streaming: boolean }) {
  return (
    <div className={styles.list}>
      {messages.map((message) =>
        message.role === 'user' ? (
          <div key={message.id} className={styles.userRow}>
            <div className={styles.userBubble}>{message.content}</div>
          </div>
        ) : (
          <div key={message.id} className={styles.assistantRow}>
            {message.content ? (
              <div className={styles.markdown}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            ) : streaming ? (
              <div className={styles.loading} aria-label="AI 正在思考">
                <span />
                <span />
                <span />
              </div>
            ) : null}
            {message.citations && message.citations.length > 0 ? (
              <CitationPanel citations={message.citations} />
            ) : null}
          </div>
        ),
      )}
    </div>
  )
}
