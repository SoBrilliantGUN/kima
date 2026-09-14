import { createBrowserRouter } from 'react-router-dom'

import App from '@/App'
import Home from '@/pages/Home'
import KnowledgeBasePage from '@/pages/KnowledgeBasePage'
import KnowledgeBaseRedirect from '@/pages/KnowledgeBaseRedirect'
import Notes from '@/pages/Notes'
import NotesRedirect from '@/pages/NotesRedirect'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Home /> },
      { path: 'knowledge-bases', element: <KnowledgeBaseRedirect /> },
      { path: 'knowledge-bases/:id', element: <KnowledgeBasePage /> },
      { path: 'notes', element: <NotesRedirect /> },
      { path: 'notes/:id', element: <Notes /> },
    ],
  },
])
