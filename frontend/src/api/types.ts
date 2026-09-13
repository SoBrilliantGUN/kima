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
