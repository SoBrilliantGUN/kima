import { KeepAliveOutlet } from '@/components/KeepAliveOutlet'
import { CopilotProvider } from '@/components/copilot/CopilotProvider'
import { CopilotWindow } from '@/components/copilot/CopilotWindow'
import { Sidebar } from '@/layout/Sidebar'

import styles from './index.module.scss'

export function AppLayout() {
  return (
    <div className={styles.layout}>
      <Sidebar />
      <main className={styles.main}>
        <CopilotProvider>
          <KeepAliveOutlet />
          <CopilotWindow />
        </CopilotProvider>
      </main>
    </div>
  )
}
