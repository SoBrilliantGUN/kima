import { createContext, useMemo, useState, type ReactNode } from 'react'

export type Theme = 'light' | 'dark'

export interface UiState {
  aiPanelOpen: boolean
  setAiPanelOpen: (open: boolean) => void
  theme: Theme
  setTheme: (theme: Theme) => void
}

export const UiContext = createContext<UiState | undefined>(undefined)

export function UiProvider({ children }: { children: ReactNode }) {
  const [aiPanelOpen, setAiPanelOpen] = useState(false)
  const [theme, setTheme] = useState<Theme>('light')

  const value = useMemo<UiState>(
    () => ({ aiPanelOpen, setAiPanelOpen, theme, setTheme }),
    [aiPanelOpen, theme],
  )

  return <UiContext.Provider value={value}>{children}</UiContext.Provider>
}
