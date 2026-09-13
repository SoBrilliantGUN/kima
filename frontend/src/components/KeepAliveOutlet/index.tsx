import { useRef, type ReactNode } from 'react'
import { useLocation, useOutlet } from 'react-router-dom'

import styles from './index.module.scss'

// 以一级路径段作为 tab key，使 /knowledge-bases/:id 的所有 id 共享同一槽位
function getTabKey(pathname: string): string {
  return pathname.split('/').filter(Boolean)[0] ?? '__home__'
}

/**
 * 缓存每个 tab 渲染出的 outlet，切换时隐藏而非卸载，从而保留各页面状态。
 * 用 visibility 而非 display:none，滚动位置也能保留。
 */
export function KeepAliveOutlet() {
  const location = useLocation()
  const outlet = useOutlet()
  const cacheRef = useRef<Map<string, ReactNode>>(new Map())

  const tabKey = getTabKey(location.pathname)

  // 渲染期同步写入缓存：set 幂等，仅保证当前 tab 的 outlet 永远最新
  if (outlet) {
    cacheRef.current.set(tabKey, outlet)
  }

  return (
    <>
      {Array.from(cacheRef.current.entries()).map(([key, element]) => (
        <div key={key} className={key === tabKey ? styles.slot : `${styles.slot} ${styles.hidden}`}>
          {element}
        </div>
      ))}
    </>
  )
}
