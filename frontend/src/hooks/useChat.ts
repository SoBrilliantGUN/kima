import { useCallback, useRef, useState } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import { getConversation, streamChat } from '@/api/chat'
import type { ChatSseEvent } from '@/api/chat'
import type { ChatMessage, Citation } from '@/api/types'

let tempIdCounter = 0

/** 生成一个临时消息 ID（服务端尚未返回真实 ID 时，用于本地渲染）。 */
function tempId(role: 'user' | 'assistant'): string {
  tempIdCounter += 1
  return `tmp-${role}-${tempIdCounter}`
}

/** 构造一条临时消息；conversation_id 尚未确定时留空。 */
function createTempMessage(role: 'user' | 'assistant', content: string, conversationId: string | null): ChatMessage {
  return {
    id: tempId(role),
    conversation_id: conversationId ?? '',
    role,
    content,
    citations: null,
    created_at: '',
  }
}

/** 给指定助手消息追加一段流式文本。 */
function appendDelta(messages: ChatMessage[], assistantId: string, text: string): ChatMessage[] {
  return messages.map((m) => (m.id === assistantId ? { ...m, content: m.content + text } : m))
}

/** 更新指定助手消息的引用列表。 */
function setCitations(messages: ChatMessage[], assistantId: string, citations: Citation[]): ChatMessage[] {
  return messages.map((m) => (m.id === assistantId ? { ...m, citations } : m))
}

/**
 * 流式问答状态：维护当前会话消息列表，发送时经 SSE 逐步追加回答文本与引用。
 * 会话的「选中/加载」由调用方通过 selectConversation/startNew 驱动。
 *
 * @param kbId      会话归属知识库（首页全局会话传 null；知识库右面板传当前库）。
 * @param kbIds     检索范围（非空 = 仅检索这些知识库，可多个）。
 * @param webSearch 联网搜索开关（kbIds 为空且 webSearch 为假 = 纯 LLM 无检索）。
 */
export function useChatStream(kbId: string | null, kbIds: string[], webSearch: boolean) {
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const queryClient = useQueryClient()

  /** 选中会话并加载其历史消息。 */
  const selectConversation = useCallback(async (id: string) => {
    setConversationId(id)
    setError(null)
    try {
      const detail = await getConversation(id)
      setMessages(detail.messages)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载会话失败')
    }
  }, [])

  /** 新建会话：清空当前状态，等待下一条消息创建会话。 */
  const startNew = useCallback(() => {
    setConversationId(null)
    setMessages([])
    setError(null)
  }, [])

  /**
   * 将一个 SSE 事件应用到组件状态：
   * meta → 记录会话/消息 ID；delta → 追加文本；citations → 更新引用；error → 设置错误。
   */
  const applyEvent = useCallback((event: ChatSseEvent, assistantId: string) => {
    switch (event.type) {
      case 'meta':
        setConversationId(event.conversationId)
        break
      case 'delta':
        setMessages((prev) => appendDelta(prev, assistantId, event.text))
        break
      case 'citations':
        setMessages((prev) => setCitations(prev, assistantId, event.citations))
        break
      case 'error':
        setError(event.message)
        break
      case 'done':
        // 流结束：无需额外处理，streaming 会在 finally 中复位
        break
    }
  }, [])

  /** 发送一条问题，经 SSE 流式接收回答。 */
  const send = useCallback(
    async (question: string) => {
      const trimmed = question.trim()
      if (streaming || !trimmed) return
      setStreaming(true)
      setError(null)

      // 无 conversationId 说明是首条消息，后端会据此新建会话
      const isNewConversation = conversationId === null

      // 乐观插入「用户问题 + 空的助手回答」，助手内容稍后逐字填充
      const userMsg = createTempMessage('user', trimmed, conversationId)
      const assistantMsg = createTempMessage('assistant', '', conversationId)
      setMessages((prev) => [...prev, userMsg, assistantMsg])

      // 每次发送新建 AbortController，供 stop() 中途取消本次流
      const controller = new AbortController()
      abortRef.current = controller
      try {
        for await (const event of streamChat(
          { kb_ids: kbIds, web_search: webSearch, kb_id: kbId, conversation_id: conversationId, question: trimmed },
          controller.signal,
        )) {
          // 首条消息 meta 返回后会话已建好，刷新会话列表让新对话立刻可见
          if (isNewConversation && event.type === 'meta') {
            queryClient.invalidateQueries({ queryKey: ['conversations'] })
          }
          applyEvent(event, assistantMsg.id)
        }
      } catch (err) {
        // AbortError 是用户主动停止，不算错误
        if (!(err instanceof DOMException && err.name === 'AbortError')) {
          setError(err instanceof Error ? err.message : '请求失败')
        }
      } finally {
        setStreaming(false)
        abortRef.current = null
      }
    },
    [kbId, kbIds, webSearch, conversationId, streaming, applyEvent, queryClient],
  )

  /** 停止当前流式回答。 */
  const stop = useCallback(() => {
    abortRef.current?.abort()
    setStreaming(false)
  }, [])

  return { conversationId, messages, streaming, error, send, stop, selectConversation, startNew }
}
