export interface HealthResponse {
  status: string
  database?: string | null
  version: string
  app: string
}
