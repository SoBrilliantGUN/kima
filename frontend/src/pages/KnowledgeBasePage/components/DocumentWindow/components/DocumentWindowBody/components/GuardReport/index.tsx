import type { GuardReport as GuardReportData } from '@/api/types'
import styles from './index.module.scss'

interface GuardReportProps {
  report: GuardReportData
}

/**
 * 写库闸命中红线的结构化展示：逐条列出命中片段，命中文字高亮，前后 100 字上下文灰显。
 * 用于文档「待确认」态，让用户在确认入库前看清到底哪里触发了注入红线。
 */
export function GuardReport({ report }: GuardReportProps) {
  return (
    <div className={styles.report}>
      <p className={styles.title}>文档疑似包含提示注入内容，请确认是否作为安全/研究文档入库：</p>
      <ul className={styles.list}>
        {report.violations.map((v, i) => (
          <li key={i} className={styles.item}>
            <span className={styles.context}>{v.before}</span>
            <mark className={styles.hit}>{v.matched}</mark>
            <span className={styles.context}>{v.after}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
