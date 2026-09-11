import { useContext } from 'react'

import { UiContext, type UiState } from '../context/UiContext'

export function useUi(): UiState {
  const context = useContext(UiContext)
  if (!context) {
    throw new Error('useUi must be used within UiProvider')
  }
  return context
}
