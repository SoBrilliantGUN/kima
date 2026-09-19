import { CloseIcon, PlusIcon } from '@/components/icons'
import type { Conversation } from '@/api/types'

import styles from './ConversationList.module.scss'

interface Props {
  conversations: Conversation[]
  activeId: string | null
  className?: string
  hideNewButton?: boolean
  onNew?: () => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
}

export function ConversationList({
  conversations,
  activeId,
  className,
  hideNewButton,
  onNew,
  onSelect,
  onDelete,
}: Props) {
  return (
    <aside className={className ? `${styles.list} ${className}` : styles.list}>
      {!hideNewButton && onNew ? (
        <button type="button" className={styles.newButton} onClick={onNew}>
          <PlusIcon className={styles.newIcon} />
          新对话
        </button>
      ) : null}
      <div className={styles.items}>
        {conversations.map((conversation) => (
          <div
            key={conversation.id}
            className={
              conversation.id === activeId ? `${styles.item} ${styles.active}` : styles.item
            }
          >
            <button
              type="button"
              className={styles.itemTitle}
              onClick={() => onSelect(conversation.id)}
              title={conversation.title}
            >
              {conversation.title}
            </button>
            <button
              type="button"
              className={styles.itemDelete}
              onClick={() => onDelete(conversation.id)}
              aria-label="删除会话"
            >
              <CloseIcon className={styles.deleteIcon} />
            </button>
          </div>
        ))}
        {conversations.length === 0 ? <div className={styles.empty}>暂无历史对话</div> : null}
      </div>
    </aside>
  )
}
