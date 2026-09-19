import { useLayoutEffect, useRef } from 'react'
import type { DependencyList } from 'react'

/**
 * 依赖变化时把挂载到的滚动容器滚到底部。
 * 用于流式对话：新消息插入 / 回答逐字追加时自动跟随最新内容。
 */
export function useAutoScroll(deps: DependencyList) {
  const ref = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    const el = ref.current
    if (el) el.scrollTop = el.scrollHeight
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return ref
}
