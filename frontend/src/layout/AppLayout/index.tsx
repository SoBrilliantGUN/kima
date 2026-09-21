import { useMemo } from 'react'
import { useLocation } from 'react-router-dom'

import type { CopilotContext } from '@/api/types'
import { KeepAliveOutlet } from '@/components/KeepAliveOutlet'
import { CopilotProvider } from '@/components/copilot/CopilotProvider'
import { CopilotWindow } from '@/components/copilot/CopilotWindow'
import { Sidebar } from '@/layout/Sidebar'

import styles from './index.module.scss'

export function AppLayout() {
  const location = useLocation()

  // 上下文感知：按路由推导当前知识库 / 笔记，随小窗 Copilot 请求提交
  const context = useMemo<CopilotContext>(() => {
    const kbMatch = location.pathname.match(/^\/knowledge-bases\/([^/]+)/)
    const noteMatch = location.pathname.match(/^\/notes\/([^/]+)/)
    return {
      kb_id: kbMatch ? kbMatch[1] : null,
      note_id: noteMatch ? noteMatch[1] : null,
      document_id: null,
    }
  }, [location.pathname])

  return (
    <div className={styles.layout}>
      <Sidebar />
      <main className={styles.main}>
        <CopilotProvider routeContext={context}>
          <KeepAliveOutlet />
          <CopilotWindow />
        </CopilotProvider>
      </main>
    </div>
  )
}
