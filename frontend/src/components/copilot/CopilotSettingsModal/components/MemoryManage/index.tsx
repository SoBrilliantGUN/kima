import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { getCopilotMemory } from '@/api/copilot'
import type { CopilotMemory } from '@/api/types'
import { CopilotAvatar } from '@/components/copilot/CopilotAvatar'

import styles from './index.module.scss'

type Tab = 'soul' | 'user' | 'longterm' | 'procedural'

const TABS: { key: Tab; label: string }[] = [
  { key: 'soul', label: 'copilot设定' },
  { key: 'user', label: '用户档案' },
  { key: 'longterm', label: '长期记忆' },
  { key: 'procedural', label: '经验技巧' },
]

function MemoryText({ text }: { text: string }) {
  if (!text.trim()) return <div className={styles.empty}>（空）</div>
  return <div className={styles.memoryText}>{text}</div>
}

function MemoryList({ memories }: { memories: CopilotMemory[] }) {
  if (memories.length === 0) return <div className={styles.empty}>（空）</div>
  return (
    <ul className={styles.memories}>
      {memories.map((memory) => (
        <li key={memory.id} className={styles.memory}>
          {memory.content}
        </li>
      ))}
    </ul>
  )
}

/** 记忆管理：形象卡片（头像 + 名字 + 编号）+ 四个纯展示记忆 Tab。 */
export function MemoryManage() {
  const [tab, setTab] = useState<Tab>('soul')

  const memoryQuery = useQuery({ queryKey: ['copilot-memory'], queryFn: getCopilotMemory })

  const memories = memoryQuery.data?.memories ?? []

  return (
    <div className={styles.manage}>
      <div className={styles.title}>记忆管理</div>

      <div className={styles.profileCard}>
        <CopilotAvatar size={44} />
        <div className={styles.profileInfo}>
          <span className={styles.profileName}>我的Copilot</span>
          <span className={styles.profileId}>9527</span>
        </div>
      </div>

      <div className={styles.tabs}>
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={tab === t.key ? `${styles.tab} ${styles.tabActive}` : styles.tab}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className={styles.tabContent}>
        {tab === 'soul' ? <MemoryText text={memoryQuery.data?.soul ?? ''} /> : null}
        {tab === 'user' ? <MemoryText text={memoryQuery.data?.user ?? ''} /> : null}
        {tab === 'longterm' ? (
          <div className={styles.groups}>
            <div className={styles.group}>
              <div className={styles.groupTitle}>硬约束</div>
              <MemoryList memories={memories.filter((m) => m.kind === 'constraint')} />
            </div>
            <div className={styles.group}>
              <div className={styles.groupTitle}>语义记忆</div>
              <MemoryList memories={memories.filter((m) => m.kind === 'semantic')} />
            </div>
            <div className={styles.group}>
              <div className={styles.groupTitle}>情节记忆</div>
              <MemoryList memories={memories.filter((m) => m.kind === 'episodic')} />
            </div>
          </div>
        ) : null}
        {tab === 'procedural' ? (
          <MemoryList memories={memories.filter((m) => m.kind === 'procedural')} />
        ) : null}
      </div>
    </div>
  )
}
