import { Modal } from '@/components/Modal'
import { useKnowledgeBases } from '@/hooks/useKnowledgeBases'
import { useAddNoteToKnowledgeBase } from '@/hooks/useNotes'
import styles from './index.module.scss'

interface AddToKnowledgeBaseModalProps {
  noteId: string
  onClose: () => void
}

export function AddToKnowledgeBaseModal({ noteId, onClose }: AddToKnowledgeBaseModalProps) {
  const { data } = useKnowledgeBases()
  const addMutation = useAddNoteToKnowledgeBase()

  const items = data?.pages.flatMap((page) => page.items) ?? []

  async function handleAdd(kbId: string) {
    await addMutation.mutateAsync({ id: noteId, payload: { knowledge_base_id: kbId } })
    onClose()
  }

  return (
    <Modal onClose={onClose} className={styles.modal}>
      <h3 className={styles.title}>添加到知识库</h3>
      {items.length === 0 ? (
        <p className={styles.empty}>还没有知识库</p>
      ) : (
        <ul className={styles.list}>
          {items.map((kb) => (
            <li key={kb.id}>
              <button
                type="button"
                className={styles.item}
                onClick={() => void handleAdd(kb.id)}
              >
                <span className={styles.cover} style={{ backgroundColor: kb.color }}>
                  {kb.name.charAt(0)}
                </span>
                <span className={styles.name}>{kb.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  )
}
