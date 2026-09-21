import { useState } from 'react'

import { isReviewStep, reviewLabel, toolLabel } from '@/api/copilot'
import type { CopilotStep } from '@/api/types'
import { ChevronDownIcon } from '@/components/icons'

import styles from './index.module.scss'

interface Props {
  steps: CopilotStep[]
}

/** 自检步骤详情：把 issues 里的 claim 拼成一句可读文案（无问题则为空）。 */
function reviewDetail(step: CopilotStep): string {
  const issues = Array.isArray(step.args?.issues) ? step.args.issues : []
  const claims = issues
    .map((issue) => String((issue as { claim?: unknown })?.claim ?? ''))
    .filter(Boolean)
  return claims.join('；')
}

/** 展开历史消息的完整工具调用链（读 chat_messages.steps 投影）。 */
export function CopilotTrace({ steps }: Props) {
  const [open, setOpen] = useState(false)
  if (steps.length === 0) return null

  return (
    <div className={styles.trace}>
      <button type="button" className={styles.toggle} onClick={() => setOpen((o) => !o)}>
        <ChevronDownIcon className={open ? `${styles.chevron} ${styles.chevronOpen}` : styles.chevron} />
        工具调用 {steps.length}
      </button>
      {open ? (
        <ol className={styles.list}>
          {steps.map((step, index) => {
            const review = isReviewStep(step)
            const name = review ? reviewLabel(String(step.args?.verdict ?? '')) : toolLabel(step)
            const args = review ? reviewDetail(step) : null
            return (
              <li key={index} className={styles.item}>
                <div className={styles.name}>{name}</div>
                {args ? <div className={styles.args}>{args}</div> : null}
              </li>
            )
          })}
        </ol>
      ) : null}
    </div>
  )
}
