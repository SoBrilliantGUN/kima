import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { ChatInput } from '@/components/chat/ChatInput'
import { TOOL_LABELS } from '@/api/copilot'
import type { ChatMessage } from '@/api/types'
import { useAutoScroll } from '@/hooks/useAutoScroll'
import { CopilotSteps } from '../CopilotSteps'
import { CopilotTrace } from '../CopilotTrace'

import styles from './index.module.scss'

interface PendingApproval {
  runId: string
  tool: string
  args: Record<string, unknown>
  summary: string
  level: string
}

function levelLabel(level: string): string {
  if (level === 'medium') return '中风险'
  if (level === 'low') return '低风险'
  return '高风险'
}

interface Props {
  messages: ChatMessage[]
  streaming: boolean
  error: string | null
  pendingApproval: PendingApproval | null
  onSend: (text: string) => void
  onStop: () => void
  onApprove: (decision: 'approve' | 'reject') => void
}

export function CopilotChat({
  messages,
  streaming,
  error,
  pendingApproval,
  onSend,
  onStop,
  onApprove,
}: Props) {
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

      {pendingApproval ? (
        <div className={styles.approval}>
          <div className={styles.approvalHeader}>
            <span
              className={`${styles.approvalBadge} ${
                pendingApproval.level === 'medium'
                  ? styles.approvalBadgeMedium
                  : styles.approvalBadgeHigh
              }`}
            >
              {levelLabel(pendingApproval.level)}
            </span>
            <span className={styles.approvalText}>
              {pendingApproval.summary ||
                `Copilot 想执行「${TOOL_LABELS[pendingApproval.tool] ?? pendingApproval.tool}」`}
            </span>
          </div>
          {Object.keys(pendingApproval.args).length > 0 ? (
            <details className={styles.approvalArgs}>
              <summary className={styles.approvalArgsSummary}>查看操作参数</summary>
              <pre className={styles.approvalArgsBody}>
                {JSON.stringify(pendingApproval.args, null, 2)}
              </pre>
            </details>
          ) : null}
          <div className={styles.approvalActions}>
            <button className={styles.approveBtn} onClick={() => onApprove('approve')}>
              确认
            </button>
            <button className={styles.rejectBtn} onClick={() => onApprove('reject')}>
              拒绝
            </button>
          </div>
        </div>
      ) : null}

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
