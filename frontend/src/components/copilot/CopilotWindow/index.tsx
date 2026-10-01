import { ArrowLeftIcon, CloseIcon, PlusIcon, SparkleIcon } from '@/components/icons'
import { ResizeHandles } from '@/components/ResizeHandles'
import { useWindowRect } from '@/hooks/useWindowRect'
import { CopilotChat } from '../CopilotChat'
import { useCopilotProvider } from '../CopilotProvider'

import styles from './index.module.scss'

/** 小窗：由主对话「小窗按钮」弹出，跨 Tab 常驻；可新建会话 / 回到主窗口 / 关闭。 */
export function CopilotWindow() {
  const { small, smallOpen, restoreToMain, closeSmall } = useCopilotProvider()
  const { style, headerHandlers, resizeHandlers } = useWindowRect(0)

  return (
    <section
      className={smallOpen ? styles.window : `${styles.window} ${styles.hidden}`}
      style={style}
    >
      <header className={styles.header} {...headerHandlers}>
        <div className={styles.titleArea}>
          <span className={styles.brand}>
            <SparkleIcon className={styles.brandIcon} />
            Copilot
          </span>
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={() => small.startNew()}
            aria-label="新建会话"
            title="新建会话"
          >
            <PlusIcon />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={restoreToMain}
            aria-label="回到主窗口"
            title="回到主窗口"
          >
            <ArrowLeftIcon />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={closeSmall}
            aria-label="关闭"
            title="关闭"
          >
            <CloseIcon />
          </button>
        </div>
      </header>

      <CopilotChat
        messages={small.messages}
        streaming={small.streaming}
        error={small.error}
        pendingApprovals={small.pendingApprovals}
        onSend={(text) => void small.send(text)}
        onStop={small.stop}
        onApprove={(id, d) => void small.approve(id, d)}
      />

      <ResizeHandles
        onPointerDown={resizeHandlers.onPointerDown}
        onPointerMove={resizeHandlers.onPointerMove}
        onPointerEnd={resizeHandlers.onPointerEnd}
      />
    </section>
  )
}
