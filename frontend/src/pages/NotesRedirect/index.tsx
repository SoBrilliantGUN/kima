import { Navigate } from 'react-router-dom'

import { recallNote } from '@/lib/lastNote'
import Notes from '@/pages/Notes'

export default function NotesRedirect() {
  const last = recallNote()
  if (last) {
    return <Navigate to={`/notes/${last}`} replace />
  }
  return <Notes />
}
