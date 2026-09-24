import { useCallback, useRef, useState } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import {
  approveCopilot,
  getCopilotConversation,
  getPendingApprovals,
  streamCopilot,
} from '@/api/copilot'
import type { CopilotSseEvent } from '@/api/copilot'
import type { ChatMessage, CopilotStep } from '@/api/types'

let tempIdCounter = 0

function tempId(role: 'user' | 'assistant'): string {
  tempIdCounter += 1
  return `tmp-copilot-${role}-${tempIdCounter}`
}

function createTempMessage(role: 'user' | 'assistant', content: string, conversationId: string | null): ChatMessage {
  return {
    id: tempId(role),
    conversation_id: conversationId ?? '',
    role,
    content,
    citations: null,
    steps: null,
    created_at: '',
  }
}

function appendDelta(messages: ChatMessage[], assistantId: string, text: string): ChatMessage[] {
  return messages.map((m) => (m.id === assistantId ? { ...m, content: m.content + text } : m))
}

function appendStep(messages: ChatMessage[], assistantId: string, step: CopilotStep): ChatMessage[] {
  return messages.map((m) =>
    m.id === assistantId ? { ...m, steps: [...(m.steps ?? []), step] } : m,
  )
}

/** 自检步骤只保留最新一条（替换旧的 review step），始终排在末尾。 */
function upsertReviewStep(messages: ChatMessage[], assistantId: string, step: CopilotStep): ChatMessage[] {
  return messages.map((m) => {
    if (m.id !== assistantId) return m
    const others = (m.steps ?? []).filter((s) => s.tool_name !== 'review')
    return { ...m, steps: [...others, step] }
  })
}

/**
 * Copilot 流式对话状态：发送后经 SSE 逐步追加回答文本与工具步骤。
 */
export function useCopilot() {
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const queryClient = useQueryClient()
  const [pendingApproval, setPendingApproval] = useState<{
    runId: string
    tool: string
    args: Record<string, unknown>
    summary: string
    level: string
    conversationId: string
    assistantMessageId: string
  } | null>(null)
  const resumeContextRef = useRef<{
    conversationId: string
    assistantMessageId: string
  } | null>(null)
  const activeAssistantIdRef = useRef<string | null>(null)

  const applyEvent = useCallback((event: CopilotSseEvent, assistantId: string) => {
    switch (event.type) {
      case 'meta':
        setConversationId(event.conversationId)
        resumeContextRef.current = {
          conversationId: event.conversationId,
          assistantMessageId: event.assistantMessageId,
        }
        break
      case 'step':
        setMessages((prev) => appendStep(prev, assistantId, { tool_name: event.toolName, args: event.args }))
        break
      case 'delta':
        setMessages((prev) => appendDelta(prev, assistantId, event.text))
        break
      case 'review':
        setMessages((prev) =>
          upsertReviewStep(prev, assistantId, {
            tool_name: 'review',
            args: { verdict: event.verdict, issues: event.issues },
          }),
        )
        break
      case 'approval':
        setPendingApproval({
          runId: event.runId,
          tool: event.tool,
          args: event.args,
          summary: event.summary,
          level: event.level,
          conversationId: resumeContextRef.current?.conversationId ?? '',
          assistantMessageId: resumeContextRef.current?.assistantMessageId ?? '',
        })
        break
      case 'error':
        setError(event.message)
        break
      case 'done':
        break
    }
  }, [])

  const send = useCallback(
    async (question: string) => {
      const trimmed = question.trim()
      if (streaming || !trimmed) return
      setStreaming(true)
      setError(null)

      const isNewConversation = conversationId === null
      const userMsg = createTempMessage('user', trimmed, conversationId)
      const assistantMsg = createTempMessage('assistant', '', conversationId)
      activeAssistantIdRef.current = assistantMsg.id
      setMessages((prev) => [...prev, userMsg, assistantMsg])

      const controller = new AbortController()
      abortRef.current = controller
      try {
        for await (const event of streamCopilot(
          { conversation_id: conversationId, question: trimmed },
          controller.signal,
        )) {
          if (isNewConversation && event.type === 'meta') {
            queryClient.invalidateQueries({ queryKey: ['conversations'] })
          }
          applyEvent(event, assistantMsg.id)
        }
      } catch (err) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) {
          setError(err instanceof Error ? err.message : '请求失败')
        }
      } finally {
        setStreaming(false)
        abortRef.current = null
      }
    },
    [conversationId, streaming, applyEvent, queryClient],
  )

  const approve = useCallback(
    async (decision: 'approve' | 'reject') => {
      const approval = pendingApproval
      if (!approval) return
      setPendingApproval(null)
      setStreaming(true)
      setError(null)

      // 确定 assistant 占位：live 审批沿用活动占位；刷新后找回的审批单没有占位，则新建一个
      let assistantId = activeAssistantIdRef.current
      if (!assistantId) {
        const assistantMsg = createTempMessage('assistant', '', approval.conversationId)
        assistantId = assistantMsg.id
        activeAssistantIdRef.current = assistantId
        setMessages((prev) => [...prev, assistantMsg])
      }

      const controller = new AbortController()
      abortRef.current = controller
      try {
        for await (const event of approveCopilot(
          {
            run_id: approval.runId,
            decision,
            conversation_id: approval.conversationId,
            assistant_message_id: approval.assistantMessageId,
          },
          controller.signal,
        )) {
          applyEvent(event, assistantId)
        }
      } catch (err) {
        if (!(err instanceof DOMException && err.name === 'AbortError')) {
          setError(err instanceof Error ? err.message : '请求失败')
        }
      } finally {
        setStreaming(false)
        abortRef.current = null
      }
    },
    [pendingApproval, applyEvent],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
    setStreaming(false)
  }, [])

  /** 找回挂起审批：按会话匹配待审单并恢复审批卡（刷新/切换会话后无感知续批）。 */
  const restoreApprovals = useCallback(async (convId: string) => {
    try {
      const { items } = await getPendingApprovals()
      const match = items.find((a) => a.conversation_id === convId)
      if (match?.conversation_id && match.assistant_message_id) {
        setPendingApproval({
          runId: match.run_id,
          tool: match.tool,
          args: match.args,
          summary: match.summary,
          level: match.level,
          conversationId: match.conversation_id,
          assistantMessageId: match.assistant_message_id,
        })
      } else {
        setPendingApproval(null)
      }
    } catch {
      // 找回失败静默降级：不阻断会话加载，仅不展示挂起审批卡
    }
  }, [])

  /** 选中会话并加载其历史消息（普通对话形态用）。 */
  const selectConversation = useCallback(
    async (id: string) => {
      if (id === conversationId) return
      abortRef.current?.abort()
      setStreaming(false)
      setConversationId(id)
      setError(null)
      setPendingApproval(null)
      activeAssistantIdRef.current = null
      resumeContextRef.current = null
      try {
        const detail = await getCopilotConversation(id)
        setMessages(detail.messages)
        void restoreApprovals(id)
      } catch (err) {
        setError(err instanceof Error ? err.message : '加载会话失败')
      }
    },
    [conversationId, restoreApprovals],
  )

  /** 新建会话：清空当前状态，等待下一条消息创建会话。 */
  const startNew = useCallback(() => {
    abortRef.current?.abort()
    setConversationId(null)
    setMessages([])
    setError(null)
    setStreaming(false)
    setPendingApproval(null)
    activeAssistantIdRef.current = null
    resumeContextRef.current = null
  }, [])

  /** 复位（悬浮窗最小化/关闭用，不加载历史）。 */
  const reset = useCallback(() => {
    abortRef.current?.abort()
    setConversationId(null)
    setMessages([])
    setError(null)
    setStreaming(false)
    setPendingApproval(null)
    activeAssistantIdRef.current = null
    resumeContextRef.current = null
  }, [])

  /** 直接灌入会话状态（弹窗/回到主窗口的转移用，不走后端拉取）。 */
  const hydrate = useCallback(
    (id: string | null, msgs: ChatMessage[]) => {
      abortRef.current?.abort()
      setConversationId(id)
      setMessages(msgs)
      setError(null)
      setStreaming(false)
      setPendingApproval(null)
      activeAssistantIdRef.current = null
      resumeContextRef.current = null
      if (id) void restoreApprovals(id)
    },
    [restoreApprovals],
  )

  return {
    conversationId,
    messages,
    streaming,
    error,
    pendingApproval,
    send,
    approve,
    stop,
    reset,
    hydrate,
    selectConversation,
    startNew,
  }
}
