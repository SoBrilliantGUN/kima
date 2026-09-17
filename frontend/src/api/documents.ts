import { api, upload } from '@/api/client'
import type { Document, DocumentContent, DocumentCreateFromUrl } from '@/api/types'

// 上传本地文件到知识库（multipart），后端创建并返回文档
export function uploadDocument(kbId: string, file: File): Promise<Document> {
  const form = new FormData()
  form.append('file', file)
  form.append('kb_id', kbId)
  return upload<Document>('/api/documents', form)
}

// 通过 URL 创建文档（后端抓取网页并解析）
export function createDocumentFromUrl(payload: DocumentCreateFromUrl): Promise<Document> {
  return api.post<Document>('/api/documents/from-url', payload)
}

// 获取单个文档详情
export function getDocument(id: string): Promise<Document> {
  return api.get<Document>(`/api/documents/${id}`)
}

// 获取文档解析后的 markdown 正文（word/url 阅读用）
export function getDocumentContent(id: string): Promise<DocumentContent> {
  return api.get<DocumentContent>(`/api/documents/${id}/content`)
}

// 解析失败后重试
export function retryDocument(id: string): Promise<Document> {
  return api.post<Document>(`/api/documents/${id}/retry`, {})
}

// 删除文档
export function deleteDocument(id: string): Promise<void> {
  return api.delete<void>(`/api/documents/${id}`)
}

// 文档原文件的访问地址（PDF 内嵌预览 / Word 下载）
export function documentFileUrl(id: string): string {
  return `/api/documents/${id}/file`
}
