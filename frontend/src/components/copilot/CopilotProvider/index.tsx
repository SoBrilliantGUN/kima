import { createContext, useContext, useState, type ReactNode } from 'react'

import type { CopilotContext } from '@/api/types'
import { useCopilot } from '@/hooks/useCopilot'

const EMPTY_CONTEXT: CopilotContext = { kb_id: null, note_id: null, document_id: null }

type Copilot = ReturnType<typeof useCopilot>

interface CopilotProviderValue {
  /** 主区域 Copilot 会话（首页，无路由上下文）。 */
  main: Copilot
  /** 小窗 Copilot 会话（跨 Tab 常驻，带路由上下文）。 */
  small: Copilot
  smallOpen: boolean
  /** 弹窗：把主区当前对话转移到小窗，主区回到空白新会话。 */
  popOut: () => void
  /** 回到主窗口：把小窗对话转回主区（覆盖主区当前空白），关小窗。 */
  restoreToMain: () => void
  /** 关闭：只关小窗，对话留在历史，不恢复。 */
  closeSmall: () => void
}

const CopilotProviderContext = createContext<CopilotProviderValue | null>(null)

export function CopilotProvider({
  routeContext,
  children,
}: {
  routeContext: CopilotContext
  children: ReactNode
}) {
  const main = useCopilot(EMPTY_CONTEXT)
  const small = useCopilot(routeContext)
  const [smallOpen, setSmallOpen] = useState(false)

  const value: CopilotProviderValue = {
    main,
    small,
    smallOpen,
    popOut: () => {
      small.hydrate(main.conversationId, main.messages)
      main.startNew()
      setSmallOpen(true)
    },
    restoreToMain: () => {
      main.hydrate(small.conversationId, small.messages)
      small.reset()
      setSmallOpen(false)
    },
    closeSmall: () => {
      small.reset()
      setSmallOpen(false)
    },
  }

  return (
    <CopilotProviderContext.Provider value={value}>{children}</CopilotProviderContext.Provider>
  )
}

export function useCopilotProvider(): CopilotProviderValue {
  const value = useContext(CopilotProviderContext)
  if (value === null) throw new Error('useCopilotProvider must be used within CopilotProvider')
  return value
}
