export interface HealthResponse {
  status: string
  database?: string | null
  version: string
  app: string
}

export interface KnowledgeBase {
  id: string
  name: string
  description: string | null
  color: string
  created_at: string
  updated_at: string
}

export interface KnowledgeBaseCreate {
  name: string
  description?: string | null
  color?: string | null
}

export interface KnowledgeBaseUpdate {
  name?: string
  description?: string | null
  color?: string | null
}

export interface KnowledgeBaseListResponse {
  items: KnowledgeBase[]
  total: number
}

export interface Note {
  id: string
  title: string
  content_markdown: string
  created_at: string
  updated_at: string
}

export interface NoteCreate {
  title?: string | null
  knowledge_base_id?: string | null
}

export interface NoteUpdate {
  title?: string
  content_markdown?: string
}

export interface NoteListResponse {
  items: Note[]
  total: number
}

export interface NoteAddToKnowledgeBase {
  knowledge_base_id: string
}

export type DocumentType = 'pdf' | 'word' | 'url'

export type DocumentStatus = 'pending' | 'processing' | 'done' | 'error' | 'needs_approval'

export interface Document {
  id: string
  kb_id: string
  title: string
  source_type: DocumentType
  source_url: string | null
  status: DocumentStatus
  embedding_approved: boolean
  error_message: string | null
  metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface DocumentCreateFromUrl {
  url: string
  knowledge_base_id: string
}

export interface DocumentContent {
  markdown: string
}

export interface ContentItem {
  type: 'note' | 'document'
  note: Note | null
  document: Document | null
}

export interface ContentListResponse {
  items: ContentItem[]
  total: number
}

export type ChatRole = 'user' | 'assistant'

export type CitationSourceType = 'document' | 'note' | 'web'

export interface Citation {
  index: number
  source_type: CitationSourceType
  source_id: string | null
  chunk_id: string | null
  title: string
  snippet: string
  url: string | null
}

export interface Conversation {
  id: string
  kb_id: string | null
  kind: string
  title: string
  created_at: string
  updated_at: string
}

export interface ConversationListResponse {
  items: Conversation[]
  total: number
}

export interface ChatMessage {
  id: string
  conversation_id: string
  role: ChatRole
  content: string
  citations: Citation[] | null
  steps?: CopilotStep[] | null
  created_at: string
}

export interface ConversationDetail {
  id: string
  kb_id: string | null
  title: string
  created_at: string
  updated_at: string
  messages: ChatMessage[]
}

export interface ChatRequest {
  kb_ids: string[]
  web_search: boolean
  kb_id?: string | null
  conversation_id?: string | null
  question: string
}

// --- Copilot（知识 Agent） ---

export type CopilotMemoryKind = 'constraint' | 'fact' | 'preference' | 'episodic'

export interface CopilotStep {
  tool_name: string
  args: Record<string, unknown>
}

export interface CopilotRequest {
  conversation_id?: string | null
  question: string
}

export interface CopilotMemory {
  id: string
  kind: CopilotMemoryKind
  content: string
  entity_id: string | null
  access_count: number
  last_access: string | null
  superseded: boolean
  version: number
  created_at: string
}

export interface CopilotMemoryList {
  soul: string
  user: string
  memories: CopilotMemory[]
}

export interface CopilotSkill {
  name: string
  description: string
  has_side_effect: boolean
}

export interface CopilotSkillsList {
  items: CopilotSkill[]
}

export interface CopilotCustomSkill {
  name: string
  description: string
  content: string
}

export interface CopilotCustomSkillsList {
  items: CopilotCustomSkill[]
}

export interface CopilotApproval {
  id: string
  run_id: string
  conversation_id: string | null
  assistant_message_id: string | null
  tool: string
  args: Record<string, unknown>
  summary: string
  level: string
  status: string
  expires_at: string | null
  created_at: string
}

export interface CopilotApprovalList {
  items: CopilotApproval[]
}

/** 前端待审审批卡（live SSE 事件 + 找回挂起审批共用）。 */
export interface CopilotPendingApproval {
  approvalId: string
  runId: string
  tool: string
  args: Record<string, unknown>
  summary: string
  level: string
  conversationId: string
  assistantMessageId: string
}
