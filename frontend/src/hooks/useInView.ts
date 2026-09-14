import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 观察元素是否进入视口，用于无限滚动列表的自动加载。
 * 返回一个 ref（挂到哨兵节点上）和是否可见的布尔值。
 *
 * 用回调 ref 而非普通 ref：哨兵节点可能在首次渲染后才挂载
 * （例如列表首屏尚在加载），回调 ref 能保证节点就绪时再建立观察。
 */
export function useInView<T extends Element>(options?: IntersectionObserverInit) {
  const [inView, setInView] = useState(false)
  const observerRef = useRef<IntersectionObserver | null>(null)

  const ref = useCallback(
    (node: T | null) => {
      observerRef.current?.disconnect()
      observerRef.current = null
      if (!node) return

      const observer = new IntersectionObserver(([entry]) => {
        setInView(entry.isIntersecting)
      }, options)
      observer.observe(node)
      observerRef.current = observer
    },
    [options],
  )

  useEffect(() => {
    return () => observerRef.current?.disconnect()
  }, [])

  return { ref, inView }
}
