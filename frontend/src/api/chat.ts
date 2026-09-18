import { api } from '@/api/client'
import type {
  ChatRequest,
  Citation,
  Conversation,
  ConversationDetail,
  ConversationListResponse,
} from '@/api/types'

export function listConversations(kbId?: string | null): Promise<ConversationListResponse> {
  const query = kbId ? `?kb_id=${encodeURIComponent(kbId)}` : ''
  return api.get<ConversationListResponse>(`/api/conversations${query}`)
}

export function createConversation(kbId?: string | null): Promise<Conversation> {
  return api.post<Conversation>('/api/conversations', { kb_id: kbId ?? null })
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return api.get<ConversationDetail>(`/api/conversations/${id}`)
}

export function deleteConversation(id: string): Promise<void> {
  return api.delete<void>(`/api/conversations/${id}`)
}

export type ChatSseEvent =
  | { type: 'meta'; conversationId: string; userMessageId: string; assistantMessageId: string }
  | { type: 'delta'; text: string }
  | { type: 'citations'; citations: Citation[] }
  | { type: 'done'; assistantMessageId: string }
  | { type: 'error'; code: string; message: string }

/** 解析一个 SSE 事件块（`event:`/`data:` 行）。 */
function parseSseBlock(block: string): ChatSseEvent | null {
  let event = ''
  let data = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice('event: '.length)
    else if (line.startsWith('data: ')) data += line.slice('data: '.length)
  }
  if (!event || !data) return null
  const payload = JSON.parse(data) as Record<string, never>
  switch (event) {
    case 'meta':
      return {
        type: 'meta',
        conversationId: String(payload.conversation_id),
        userMessageId: String(payload.user_message_id),
        assistantMessageId: String(payload.assistant_message_id),
      }
    case 'delta':
      return { type: 'delta', text: String(payload.text) }
    case 'citations':
      return { type: 'citations', citations: payload as unknown as Citation[] }
    case 'done':
      return { type: 'done', assistantMessageId: String(payload.assistant_message_id) }
    case 'error':
      return {
        type: 'error',
        code: String(payload.code ?? ''),
        message: String(payload.message ?? ''),
      }
    default:
      return null
  }
}

/** 流式问答：POST /api/chat，逐事件 yield（meta/delta/citations/done/error）。 */
export async function* streamChat(
  request: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<ChatSseEvent> {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
    signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`请求失败（${response.status}）`)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() ?? ''
    for (const block of blocks) {
      const event = parseSseBlock(block)
      if (event) yield event
    }
  }
}
