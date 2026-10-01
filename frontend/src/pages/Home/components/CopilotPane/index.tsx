import { useState } from 'react'

import { CopilotChat } from '@/components/copilot/CopilotChat'
import { CopilotSettingsModal } from '@/components/copilot/CopilotSettingsModal'
import { useCopilotProvider } from '@/components/copilot/CopilotProvider'
import { GearIcon, WindowIcon } from '@/components/icons'

import styles from './index.module.scss'

/** 首页主区域的 Copilot 对话：右上角 [齿轮(copilot设置)] [小窗按钮]。 */
export function CopilotPane() {
  const { main, popOut } = useCopilotProvider()
  const [settingsOpen, setSettingsOpen] = useState(false)

  return (
    <div className={styles.pane}>
      <header className={styles.header}>
        <span className={styles.title}>我的Copilot</span>
        <div className={styles.actions}>
          <button
            type="button"
            className={settingsOpen ? `${styles.iconBtn} ${styles.iconBtnActive}` : styles.iconBtn}
            onClick={() => setSettingsOpen((open) => !open)}
            aria-label="copilot设置"
            title="copilot设置"
          >
            <GearIcon />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={popOut}
            aria-label="小窗"
            title="小窗"
          >
            <WindowIcon />
          </button>
        </div>
      </header>

      <CopilotChat
        messages={main.messages}
        streaming={main.streaming}
        error={main.error}
        pendingApprovals={main.pendingApprovals}
        onSend={(text) => void main.send(text)}
        onStop={main.stop}
        onApprove={(id, d) => void main.approve(id, d)}
      />

      {settingsOpen ? <CopilotSettingsModal onClose={() => setSettingsOpen(false)} /> : null}
    </div>
  )
}
