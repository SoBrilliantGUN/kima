// dev 下经 Vite proxy 转发到后端；生产可改为 import.meta.env.VITE_API_BASE_URL
const BASE_URL = ''

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

interface ErrorDetail {
  code?: string
  message?: string
}

function extractError(body: unknown, fallback: string): { message: string; code?: string } {
  if (typeof body === 'object' && body !== null) {
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') {
      return { message: detail }
    }
    if (typeof detail === 'object' && detail !== null && !Array.isArray(detail)) {
      const { code, message } = detail as ErrorDetail
      return { message: message ?? fallback, code }
    }
  }
  return { message: fallback }
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => undefined)
    const { message, code } = extractError(body, response.statusText)
    throw new ApiError(response.status, message, code)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return response.json() as Promise<T>
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers,
  })
  return handleResponse<T>(response)
}

// multipart 上传：不手动设 Content-Type（交给浏览器带 boundary）
export async function upload<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, { method: 'POST', body })
  return handleResponse<T>(response)
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
