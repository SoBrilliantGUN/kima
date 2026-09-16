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

export type DocumentStatus = 'pending' | 'processing' | 'done' | 'error'

export interface Document {
  id: string
  kb_id: string
  title: string
  source_type: DocumentType
  source_url: string | null
  status: DocumentStatus
  error_message: string | null
  metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface DocumentCreateFromUrl {
  url: string
  knowledge_base_id: string
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
