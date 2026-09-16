import { useEffect, type ReactNode } from 'react'

import styles from './index.module.scss'

interface ModalProps {
  onClose: () => void
  children: ReactNode
  // 附加到面板，覆盖宽度/高度/overflow 等各模态差异
  className?: string
}

/**
 * 最小模态原语：只负责「居中浮层 + 点遮罩关闭 + Escape 关闭 + 面板 stopPropagation」。
 * 标题/header/关闭按钮/面板尺寸均由各模态自行渲染。
 */
export function Modal({ onClose, children, className }: ModalProps) {
  useEffect(() => {
    function handleKeydown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeydown)
    return () => window.removeEventListener('keydown', handleKeydown)
  }, [onClose])

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div
        className={className ? `${styles.modal} ${className}` : styles.modal}
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}
