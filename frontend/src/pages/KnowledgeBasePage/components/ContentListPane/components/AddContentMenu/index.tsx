import { useEffect, useRef } from 'react'

import styles from './index.module.scss'

interface AddContentMenuProps {
  onSelect: (type: 'url' | 'document') => void
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
      <button type="button" onClick={() => onSelect('document')}>
        本地文档
      </button>
      <button type="button" onClick={() => onSelect('url')}>
        URL 文档
      </button>
    </div>
  )
}
