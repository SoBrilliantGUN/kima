import {
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react'

export type ResizeDirection = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'

interface Rect {
  x: number
  y: number
  width: number
  height: number
}

// 默认尺寸 min(72vw, 900px) × min(78vh, 720px)
const SIDEBAR_WIDTH = 64
const EDGE_MARGIN = 12
const MIN_WIDTH = 320
const MIN_HEIGHT = 240

function clamp(value: number, cap: number): number {
  return Math.min(value, cap)
}

// 保证窗口整体留在内容区（右侧栏内容区域，左侧有 SIDEBAR_WIDTH 宽的全局侧边栏）内，
// 四周留 EDGE_MARGIN 边距，使 header 与所有缩放手柄始终可见、可操作。
// 注意：window 的 x 是相对内容区左边缘（视口 x=64），而非整个视口，
// 因此横向上限要用 contentWidth 而不是 window.innerWidth。
function clampToViewport(rect: Rect): Rect {
  const contentWidth = window.innerWidth - SIDEBAR_WIDTH
  const maxWidth = contentWidth - EDGE_MARGIN * 2
  const maxHeight = window.innerHeight - EDGE_MARGIN * 2
  const width = Math.max(MIN_WIDTH, Math.min(rect.width, maxWidth))
  const height = Math.max(MIN_HEIGHT, Math.min(rect.height, maxHeight))
  const maxX = Math.max(EDGE_MARGIN, contentWidth - EDGE_MARGIN - width)
  const maxY = Math.max(EDGE_MARGIN, window.innerHeight - EDGE_MARGIN - height)
  return {
    x: Math.min(Math.max(rect.x, EDGE_MARGIN), maxX),
    y: Math.min(Math.max(rect.y, EDGE_MARGIN), maxY),
    width,
    height,
  }
}

// 根据缩放方向与位移，从 origin 出发计算新矩形。
// 只移动对应方向的边，锚定边保持不动；先保证最小尺寸，
// 再把「移动的那条边」钳制到内容区内，避免拖某条边到边界时反而把对侧边推开。
function applyResize(origin: Rect, direction: ResizeDirection, dx: number, dy: number): Rect {
  const contentWidth = window.innerWidth - SIDEBAR_WIDTH
  let left = origin.x
  let top = origin.y
  let right = origin.x + origin.width
  let bottom = origin.y + origin.height

  if (direction.includes('e')) right += dx
  if (direction.includes('w')) left += dx
  if (direction.includes('s')) bottom += dy
  if (direction.includes('n')) top += dy

  // 最小尺寸：缩得太小时拉回 MIN_WIDTH/MIN_HEIGHT，且只移动「被拖的那条边」，
  // 锚定边不动——拖左边缘固定右边、拖右边缘固定左边（高度同理）。
  if (right - left < MIN_WIDTH) {
    if (direction.includes('w')) left = right - MIN_WIDTH
    else right = left + MIN_WIDTH
  }
  if (bottom - top < MIN_HEIGHT) {
    if (direction.includes('n')) top = bottom - MIN_HEIGHT
    else bottom = top + MIN_HEIGHT
  }

  // 钳制移动边：左/上边不越过 EDGE_MARGIN，右/下边不越过内容区边界 - EDGE_MARGIN
  if (direction.includes('w')) left = Math.max(left, EDGE_MARGIN)
  if (direction.includes('e')) right = Math.min(right, contentWidth - EDGE_MARGIN)
  if (direction.includes('n')) top = Math.max(top, EDGE_MARGIN)
  if (direction.includes('s')) bottom = Math.min(bottom, window.innerHeight - EDGE_MARGIN)

  return { x: left, y: top, width: right - left, height: bottom - top }
}

// 计算初始位置：居中后按 offset 往右下偏移，让多个窗口像卡片一样错开而非完全重叠。
// offset 只影响初始位置（来自 useDocumentWindows 的 prev.length * 24），拖动/缩放后不再参与。
function initialRect(offset: number): Rect {
  const contentWidth = window.innerWidth - SIDEBAR_WIDTH
  const width = clamp(contentWidth * 0.72, 900)
  const height = clamp(window.innerHeight * 0.78, 720)
  const x = Math.max(EDGE_MARGIN, (contentWidth - width) / 2 + offset)
  const y = Math.max(EDGE_MARGIN, (window.innerHeight - height) / 2 + offset)
  return clampToViewport({ x, y, width, height })
}

interface DragState {
  pointerId: number
  startX: number
  startY: number
  originX: number
  originY: number
}

interface ResizeState {
  pointerId: number
  direction: ResizeDirection
  startX: number
  startY: number
  origin: Rect
}

/**
 * 浮动窗口的位置与尺寸管理：拖动 header 移动、8 方向缩放，
 * 全程钳制在视口内，保证 header 与缩放手柄始终可操作。
 */
export function useWindowRect(offset: number) {
  const [rect, setRect] = useState<Rect>(() => initialRect(offset))
  // 拖拽/缩放进行中的状态，用 ref 而非 state：pointermove 高频触发，ref 不引起重渲染。
  // 记下 pointerId 与按下时的起点坐标/原点，供 move 时计算位移增量。
  const dragRef = useRef<DragState | null>(null)
  const resizeRef = useRef<ResizeState | null>(null)

  function handleHeaderPointerDown(event: ReactPointerEvent<HTMLElement>) {
    if ((event.target as HTMLElement).closest('button')) return
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: rect.x,
      originY: rect.y,
    }
    // 捕获指针：光标拖出 header 甚至窗口，pointermove 仍发给这里，拖动不中断
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function handleHeaderPointerMove(event: ReactPointerEvent<HTMLElement>) {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    setRect((r) =>
      clampToViewport({
        ...r,
        x: drag.originX + (event.clientX - drag.startX),
        y: drag.originY + (event.clientY - drag.startY),
      })
    )
  }

  function handleHeaderPointerEnd(event: ReactPointerEvent<HTMLElement>) {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null
  }

  function handleResizePointerDown(direction: ResizeDirection) {
    return (event: ReactPointerEvent<HTMLElement>) => {
      event.stopPropagation()
      resizeRef.current = {
        pointerId: event.pointerId,
        direction,
        startX: event.clientX,
        startY: event.clientY,
        origin: rect,
      }
      // 手柄只有 8~16px，一拖动光标就出界；捕获指针保证后续 move 仍发给手柄
      event.currentTarget.setPointerCapture(event.pointerId)
    }
  }

  function handleResizePointerMove(event: ReactPointerEvent<HTMLElement>) {
    const resize = resizeRef.current
    if (!resize || resize.pointerId !== event.pointerId) return
    const dx = event.clientX - resize.startX
    const dy = event.clientY - resize.startY
    setRect(applyResize(resize.origin, resize.direction, dx, dy))
  }

  function handleResizePointerEnd(event: ReactPointerEvent<HTMLElement>) {
    if (resizeRef.current?.pointerId === event.pointerId) resizeRef.current = null
  }

  const style: CSSProperties = {
    left: rect.x,
    top: rect.y,
    width: rect.width,
    height: rect.height,
  }

  return {
    style,
    headerHandlers: {
      onPointerDown: handleHeaderPointerDown,
      onPointerMove: handleHeaderPointerMove,
      onPointerUp: handleHeaderPointerEnd,
      onPointerCancel: handleHeaderPointerEnd,
    },
    resizeHandlers: {
      onPointerDown: handleResizePointerDown,
      onPointerMove: handleResizePointerMove,
      onPointerEnd: handleResizePointerEnd,
    },
  }
}
