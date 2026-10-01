import { useCallback, useRef, useState } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import {
  approveCopilot,
  getCopilotConversation,
  getPendingApprovals,
  streamCopilot,
} from '@/api/copilot'
import type { CopilotSseEvent } from '@/api/copilot'
import type { ChatMessage, CopilotPendingApproval, CopilotStep } from '@/api/types'

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
  // 待审审批单队列：并行多 interrupt 时一次可挂多张，逐张各自裁决（不再一刀切）。
  const [pendingApprovals, setPendingApprovals] = useState<CopilotPendingApproval[]>([])
  // 逐单裁决的累加器：全部裁完才一次性 resume（LangGraph 多 interrupt 需原子续跑）。
  const decisionsRef = useRef<Map<string, 'approve' | 'reject'>>(new Map())
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
        setPendingApprovals((prev) => {
          if (prev.some((a) => a.approvalId === event.approvalId)) return prev
          return [
            ...prev,
            {
              approvalId: event.approvalId,
              runId: event.runId,
              tool: event.tool,
              args: event.args,
              summary: event.summary,
              level: event.level,
              conversationId: resumeContextRef.current?.conversationId ?? '',
              assistantMessageId: resumeContextRef.current?.assistantMessageId ?? '',
            },
          ]
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
    async (approvalId: string, decision: 'approve' | 'reject') => {
      const target = pendingApprovals.find((a) => a.approvalId === approvalId)
      if (!target) return
      decisionsRef.current.set(approvalId, decision)
      const remaining = pendingApprovals.filter((a) => a.approvalId !== approvalId)
      setPendingApprovals(remaining)
      // 还有待裁决的单：先记下裁决，等全部裁完再一次性 resume（多 interrupt 需原子续跑）。
      if (remaining.length > 0) return

      const decisions = Array.from(decisionsRef.current.entries()).map(([id, d]) => ({
        approval_id: id,
        decision: d,
      }))
      decisionsRef.current.clear()
      setStreaming(true)
      setError(null)

      // 确定 assistant 占位：live 审批沿用活动占位；刷新后找回的审批单没有占位，则新建一个
      let assistantId = activeAssistantIdRef.current
      if (!assistantId) {
        const assistantMsg = createTempMessage('assistant', '', target.conversationId)
        assistantId = assistantMsg.id
        activeAssistantIdRef.current = assistantId
        setMessages((prev) => [...prev, assistantMsg])
      }

      const controller = new AbortController()
      abortRef.current = controller
      try {
        for await (const event of approveCopilot(
          {
            run_id: target.runId,
            decisions,
            conversation_id: target.conversationId,
            assistant_message_id: target.assistantMessageId,
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
    [pendingApprovals, applyEvent],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
    setStreaming(false)
  }, [])

  /** 找回挂起审批：按会话匹配待审单并恢复审批卡（刷新/切换会话后无感知续批）。 */
  const restoreApprovals = useCallback(async (convId: string) => {
    try {
      const { items } = await getPendingApprovals()
      const matches = items.filter(
        (a) => a.conversation_id === convId && a.assistant_message_id,
      )
      setPendingApprovals(
        matches.map((a) => ({
          approvalId: a.id,
          runId: a.run_id,
          tool: a.tool,
          args: a.args,
          summary: a.summary,
          level: a.level,
          conversationId: a.conversation_id ?? '',
          assistantMessageId: a.assistant_message_id ?? '',
        })),
      )
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
      setPendingApprovals([])
      decisionsRef.current.clear()
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
    setPendingApprovals([])
    decisionsRef.current.clear()
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
    setPendingApprovals([])
    decisionsRef.current.clear()
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
      setPendingApprovals([])
      decisionsRef.current.clear()
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
    pendingApprovals,
    send,
    approve,
    stop,
    reset,
    hydrate,
    selectConversation,
    startNew,
  }
}
