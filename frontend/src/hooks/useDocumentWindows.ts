import { useCallback, useState } from 'react'

// 新窗口相对上一个再偏移的级联距离（px）
const CASCADE = 24

export interface OpenDocumentWindow {
  documentId: string
  zIndex: number
  offset: number
}

/**
 * 浮动文档窗口的纯内存态管理（刷新即消失）。
 * - open：已打开同文档则去重聚焦（置顶），否则新增
 * - focus：把指定窗口置顶
 * - close：关闭指定窗口
 * - clear：清空全部（切换知识库时调用）
 */
export function useDocumentWindows() {
  const [windows, setWindows] = useState<OpenDocumentWindow[]>([])

  const open = useCallback((documentId: string) => {
    setWindows((prev) => {
      const top = prev.reduce((max, w) => Math.max(max, w.zIndex), 0)
      const existing = prev.find((w) => w.documentId === documentId)
      if (existing) {
        return prev.map((w) =>
          w.documentId === documentId ? { ...w, zIndex: top + 1 } : w,
        )
      }
      return [...prev, { documentId, zIndex: top + 1, offset: prev.length * CASCADE }]
    })
  }, [])

  const focus = useCallback((documentId: string) => {
    setWindows((prev) => {
      const top = prev.reduce((max, w) => Math.max(max, w.zIndex), 0)
      const target = prev.find((w) => w.documentId === documentId)
      if (!target || target.zIndex === top) return prev
      return prev.map((w) =>
        w.documentId === documentId ? { ...w, zIndex: top + 1 } : w,
      )
    })
  }, [])

  const close = useCallback((documentId: string) => {
    setWindows((prev) => prev.filter((w) => w.documentId !== documentId))
  }, [])

  const clear = useCallback(() => {
    setWindows([])
  }, [])

  return { windows, open, focus, close, clear }
}
