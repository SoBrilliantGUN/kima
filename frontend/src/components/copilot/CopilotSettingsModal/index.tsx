import { useState } from 'react'

import { CloseIcon, DatabaseIcon, GridIcon } from '@/components/icons'
import { Modal } from '@/components/Modal'
import { MemoryManage } from './components/MemoryManage'
import { SkillsManage } from './components/SkillsManage'

import styles from './index.module.scss'

interface Props {
  onClose: () => void
}

type Section = 'memory' | 'skills'

/** Copilot 设置弹窗：标题栏 + 左侧记忆管理 / Skills管理 + 右侧对应内容。 */
export function CopilotSettingsModal({ onClose }: Props) {
  const [section, setSection] = useState<Section>('memory')

  return (
    <Modal onClose={onClose} className={styles.modal}>
      <header className={styles.header}>
        <span className={styles.headerTitle}>Copilot 设置</span>
        <button type="button" className={styles.close} onClick={onClose} aria-label="关闭">
          <CloseIcon />
        </button>
      </header>

      <div className={styles.body}>
        <nav className={styles.nav}>
          <button
            type="button"
            className={section === 'memory' ? `${styles.navItem} ${styles.navActive}` : styles.navItem}
            onClick={() => setSection('memory')}
          >
            <DatabaseIcon className={styles.navIcon} />
            记忆管理
          </button>
          <button
            type="button"
            className={section === 'skills' ? `${styles.navItem} ${styles.navActive}` : styles.navItem}
            onClick={() => setSection('skills')}
          >
            <GridIcon className={styles.navIcon} />
            Skills管理
          </button>
        </nav>
        <div className={styles.content}>
          {section === 'memory' ? <MemoryManage /> : <SkillsManage />}
        </div>
      </div>
    </Modal>
  )
}
