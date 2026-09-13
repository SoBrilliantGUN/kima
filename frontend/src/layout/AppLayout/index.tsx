import { Outlet } from 'react-router-dom'

import styles from './index.module.scss'
import { Sidebar } from '@/layout/Sidebar'

export function AppLayout() {
  return (
    <div className={styles.layout}>
      <Sidebar />
      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  )
}
