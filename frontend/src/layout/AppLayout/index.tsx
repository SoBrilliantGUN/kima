import { KeepAliveOutlet } from '@/components/KeepAliveOutlet'
import { Sidebar } from '@/layout/Sidebar'

import styles from './index.module.scss'

export function AppLayout() {
  return (
    <div className={styles.layout}>
      <Sidebar />
      <main className={styles.main}>
        <KeepAliveOutlet />
      </main>
    </div>
  )
}
