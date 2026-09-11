import { Outlet } from 'react-router-dom'

import { useUi } from '../hooks/useUi'
import styles from './AppLayout.module.scss'
import { AiPanel } from './AiPanel'
import { Sidebar } from './Sidebar'

export function AppLayout() {
  const { aiPanelOpen, setAiPanelOpen } = useUi()

  return (
    <div className={styles.layout}>
      <Sidebar />
      <main className={styles.main}>
        <header className={styles.topbar}>
          <button
            type="button"
            className={styles.toggle}
            onClick={() => setAiPanelOpen(!aiPanelOpen)}
          >
            AI
          </button>
        </header>
        <div className={styles.content}>
          <Outlet />
        </div>
      </main>
      <AiPanel />
    </div>
  )
}
