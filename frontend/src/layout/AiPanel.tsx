import { useUi } from '../hooks/useUi'
import styles from './AiPanel.module.scss'

export function AiPanel() {
  const { aiPanelOpen } = useUi()

  const className = aiPanelOpen ? `${styles.panel} ${styles.open}` : styles.panel

  return (
    <aside className={className}>
      <div className={styles.header}>AI 问答</div>
      <div className={styles.body}>占位：AI 侧栏（模块 5 接入）</div>
    </aside>
  )
}
