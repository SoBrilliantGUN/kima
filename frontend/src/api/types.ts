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

export type NoteType = 'markdown' | 'url'

export interface Note {
  id: string
  title: string
  type: NoteType
  content_markdown: string
  summary: string | null
  source_url: string | null
  created_at: string
  updated_at: string
}

export interface NoteCreateFromUrl {
  url: string
  knowledge_base_id?: string | null
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

export interface ContentItem {
  type: 'note'
  note: Note | null
}

export interface ContentListResponse {
  items: ContentItem[]
  total: number
}
