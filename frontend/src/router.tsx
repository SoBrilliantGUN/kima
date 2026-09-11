import { createBrowserRouter, Navigate } from 'react-router-dom'

import App from './App'
import Chat from './pages/Chat'
import KnowledgeBases from './pages/KnowledgeBases'
import Notes from './pages/Notes'
import Search from './pages/Search'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/knowledge-bases" replace /> },
      { path: 'knowledge-bases', element: <KnowledgeBases /> },
      { path: 'notes', element: <Notes /> },
      { path: 'search', element: <Search /> },
      { path: 'chat', element: <Chat /> },
    ],
  },
])
