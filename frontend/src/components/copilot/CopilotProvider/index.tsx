import { createContext, useContext, useState, type ReactNode } from 'react'

import { useCopilot } from '@/hooks/useCopilot'

type Copilot = ReturnType<typeof useCopilot>

interface CopilotProviderValue {
  /** 主区域 Copilot 会话（首页）。 */
  main: Copilot
  /** 小窗 Copilot 会话（跨 Tab 常驻）。 */
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

export function CopilotProvider({ children }: { children: ReactNode }) {
  const main = useCopilot()
  const small = useCopilot()
  const [smallOpen, setSmallOpen] = useState(false)

  const value: CopilotProviderValue = {
    main,
    small,
    smallOpen,
    popOut: () => {
      const convId = main.conversationId
      const msgs = main.messages
      const wasStreaming = main.streaming
      const last = msgs[msgs.length - 1]
      small.hydrate(convId, msgs) // 立即用快照显示
      main.startNew() // 断主区流（后台 run 继续，不取消）
      setSmallOpen(true)
      // 主区还在输出 → 小窗接续：开流回放 + tail，让 AI 继续在小窗里输出
      if (convId && last && last.role === 'assistant' && wasStreaming) {
        void small.resume(last.id, convId)
      }
    },
    restoreToMain: () => {
      const convId = small.conversationId
      const msgs = small.messages
      const wasStreaming = small.streaming
      const last = msgs[msgs.length - 1]
      main.hydrate(convId, msgs)
      small.reset()
      setSmallOpen(false)
      if (convId && last && last.role === 'assistant' && wasStreaming) {
        void main.resume(last.id, convId)
      }
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
