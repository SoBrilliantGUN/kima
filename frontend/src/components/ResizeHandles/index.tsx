import type { PointerEvent as ReactPointerEvent } from 'react'

import type { ResizeDirection } from '@/hooks/useWindowRect'
import styles from './index.module.scss'

const DIRECTIONS: { dir: ResizeDirection; className: string }[] = [
  { dir: 'n', className: styles.resizeN },
  { dir: 's', className: styles.resizeS },
  { dir: 'e', className: styles.resizeE },
  { dir: 'w', className: styles.resizeW },
  { dir: 'ne', className: styles.resizeNe },
  { dir: 'nw', className: styles.resizeNw },
  { dir: 'se', className: styles.resizeSe },
  { dir: 'sw', className: styles.resizeSw },
]

interface ResizeHandlesProps {
  onPointerDown: (direction: ResizeDirection) => (event: ReactPointerEvent<HTMLElement>) => void
  onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void
  onPointerEnd: (event: ReactPointerEvent<HTMLElement>) => void
}

export function ResizeHandles({ onPointerDown, onPointerMove, onPointerEnd }: ResizeHandlesProps) {
  return (
    <>
      {DIRECTIONS.map(({ dir, className }) => (
        <div
          key={dir}
          className={`${styles.resizeHandle} ${className}`}
          onPointerDown={onPointerDown(dir)}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd}
        />
      ))}
    </>
  )
}
