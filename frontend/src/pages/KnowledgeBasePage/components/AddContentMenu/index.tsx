import { useEffect, useRef } from 'react'

import styles from './index.module.scss'

interface AddContentMenuProps {
  onSelect: (type: 'web' | 'note') => void
  onClose: () => void
}

export function AddContentMenu({ onSelect, onClose }: AddContentMenuProps) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleMouseDown(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose()
    }
    window.addEventListener('mousedown', handleMouseDown)
    return () => window.removeEventListener('mousedown', handleMouseDown)
  }, [onClose])

  return (
    <div className={styles.menu} ref={ref}>
      <button type="button" onClick={() => onSelect('web')}>
        网页
      </button>
      <button type="button" onClick={() => onSelect('note')}>
        笔记
      </button>
      <button type="button" disabled title="模块 4 支持">
        本地文档
      </button>
    </div>
  )
}
