import { api } from '@/api/client'
import type {
  ConversationDetail,
  CopilotMemoryList,
  CopilotRequest,
  CopilotSkillsList,
  CopilotStep,
} from '@/api/types'

export type CopilotReviewIssue = { claim: string; tool: string; evidence: string }

export type CopilotSseEvent =
  | { type: 'meta'; conversationId: string; userMessageId: string; assistantMessageId: string }
  | { type: 'step'; toolName: string; args: Record<string, unknown> }
  | { type: 'delta'; text: string }
  | { type: 'review'; verdict: string; issues: CopilotReviewIssue[] }
  | {
      type: 'approval'
      runId: string
      tool: string
      args: Record<string, unknown>
      summary: string
      level: string
    }
  | { type: 'done'; assistantMessageId: string }
  | { type: 'error'; code: string; message: string }

/** 解析一个 SSE 事件块（`event:`/`data:` 行）。 */
function parseSseBlock(block: string): CopilotSseEvent | null {
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
    case 'step':
      return {
        type: 'step',
        toolName: String(payload.tool_name),
        args: (payload.args ?? {}) as Record<string, unknown>,
      }
    case 'delta':
      return { type: 'delta', text: String(payload.text) }
    case 'review':
      return {
        type: 'review',
        verdict: String(payload.verdict),
        issues: Array.isArray(payload.issues) ? (payload.issues as CopilotReviewIssue[]) : [],
      }
    case 'approval':
      return {
        type: 'approval',
        runId: String(payload.run_id),
        tool: String(payload.tool),
        args: (payload.args ?? {}) as Record<string, unknown>,
        summary: String(payload.summary ?? ''),
        level: String(payload.level ?? 'high'),
      }
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

/** 通用 SSE 流式：POST 到 url，逐事件 yield（meta/step/delta/review/approval/done/error）。 */
async function* streamSse(
  url: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<CopilotSseEvent> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
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

/** 流式 Copilot 对话：POST /api/copilot/chat。 */
export function streamCopilot(request: CopilotRequest, signal?: AbortSignal) {
  return streamSse('/api/copilot/chat', request, signal)
}

export interface CopilotApproveRequest {
  run_id: string
  decision: 'approve' | 'reject'
  conversation_id: string
  assistant_message_id: string
}

/** HITL 审批回执：POST /api/copilot/approve，续跑。 */
export function approveCopilot(request: CopilotApproveRequest, signal?: AbortSignal) {
  return streamSse('/api/copilot/approve', request, signal)
}

/** 只读记忆面板数据。 */
export function getCopilotMemory(): Promise<CopilotMemoryList> {
  return api.get<CopilotMemoryList>('/api/copilot/memory')
}

/** 内置技能清单。 */
export function getCopilotSkills(): Promise<CopilotSkillsList> {
  return api.get<CopilotSkillsList>('/api/copilot/skills')
}

/** Copilot 会话详情（含历史消息，含 steps 工具轨迹）。 */
export function getCopilotConversation(id: string): Promise<ConversationDetail> {
  return api.get<ConversationDetail>(`/api/conversations/${id}`)
}

/** 工具名 → 人类可读动作文案（CopilotSteps / CopilotTrace 用）。 */
export const TOOL_LABELS: Record<string, string> = {
  search_knowledge_base: '正在检索知识库',
  list_knowledge_bases: '正在列出知识库',
  list_notes: '正在列出笔记',
  read_document: '正在读取文档',
  read_note: '正在读取笔记',
  search_web: '正在联网搜索',
  search_memory: '正在读取记忆',
  create_note: '正在新建笔记',
  write_memory: '正在写入记忆',
}

export function toolLabel(step: CopilotStep): string {
  return TOOL_LABELS[step.tool_name] ?? step.tool_name
}

/** 合成审查步骤的 tool_name（后端 chat_messages.steps 投影里标记自检节点）。 */
export const REVIEW_TOOL_NAME = 'review'

const REVIEW_LABELS: Record<string, string> = {
  ok: '自检通过',
  mismatch: '自检发现问题，正在补做',
  repaired: '自检发现问题，已自动补做',
  corrected: '自检发现问题，已更正说明',
}

export function reviewLabel(verdict: string): string {
  return REVIEW_LABELS[verdict] ?? '自检'
}

export function isReviewStep(step: CopilotStep): boolean {
  return step.tool_name === REVIEW_TOOL_NAME
}
