import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { HealthResponse } from '@/api/types'

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => api.get<HealthResponse>('/health/ready'),
    refetchInterval: 30_000,
  })
}
