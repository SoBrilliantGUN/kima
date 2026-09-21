import { isReviewStep, reviewLabel, toolLabel } from '@/api/copilot'
import type { CopilotStep } from '@/api/types'

import styles from './index.module.scss'

interface Props {
  steps: CopilotStep[]
  streaming: boolean
}

/** 把工具调用渲染成「正在检索知识库 / 读取文档 …」折叠步骤条（流式期间展示）。 */
export function CopilotSteps({ steps, streaming }: Props) {
  if (steps.length === 0) return null
  return (
    <div className={styles.steps}>
      {steps.map((step, index) => {
        const isLast = index === steps.length - 1
        const active = isLast && streaming
        const review = isReviewStep(step)
        const label = review ? reviewLabel(String(step.args?.verdict ?? '')) : toolLabel(step)
        const classes = [styles.step, review && styles.review, active && styles.active]
          .filter(Boolean)
          .join(' ')
        return (
          <div key={index} className={classes}>
            <span className={styles.dot} />
            <span className={styles.label}>{label}</span>
          </div>
        )
      })}
    </div>
  )
}
