import { api } from '@/api/client'
import type {
  Note,
  NoteAddToKnowledgeBase,
  NoteCreate,
  NoteCreateFromUrl,
  NoteListResponse,
  NoteUpdate,
} from '@/api/types'

export function createNoteFromUrl(payload: NoteCreateFromUrl): Promise<Note> {
  return api.post<Note>('/api/notes/from-url', payload)
}

export function createNote(payload: NoteCreate): Promise<Note> {
  return api.post<Note>('/api/notes', payload)
}

export function listNotes(limit = 50, offset = 0): Promise<NoteListResponse> {
  return api.get<NoteListResponse>(`/api/notes?limit=${limit}&offset=${offset}`)
}

export function getNote(id: string): Promise<Note> {
  return api.get<Note>(`/api/notes/${id}`)
}

export function updateNote(id: string, payload: NoteUpdate): Promise<Note> {
  return api.patch<Note>(`/api/notes/${id}`, payload)
}

export function deleteNote(id: string): Promise<void> {
  return api.delete<void>(`/api/notes/${id}`)
}

export function addNoteToKnowledgeBase(id: string, payload: NoteAddToKnowledgeBase): Promise<void> {
  return api.post<void>(`/api/notes/${id}/knowledge-bases`, payload)
}
