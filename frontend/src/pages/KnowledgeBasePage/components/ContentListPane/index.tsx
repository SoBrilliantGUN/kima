import { FilePlusIcon, SearchIcon } from '@/components/icons'
import { useKnowledgeBase } from '@/hooks/useKnowledgeBases'
import styles from './index.module.scss'

interface ContentListPaneProps {
  kbId?: string
}

export function ContentListPane({ kbId }: ContentListPaneProps) {
  const { data: kb, isLoading, isError } = useKnowledgeBase(kbId)

  if (isLoading) {
    return <section className={styles.pane}>加载中…</section>
  }
  if (isError || !kb) {
    return <section className={styles.pane}>知识库不存在或已删除。</section>
  }

  return (
    <section className={styles.pane}>
      <header className={styles.header}>
        <span className={styles.cover} style={{ backgroundColor: kb.color }}>
          {kb.name.charAt(0)}
        </span>
        <div className={styles.heading}>
          <h2 className={styles.name}>{kb.name}</h2>
          {kb.description ? <p className={styles.description}>{kb.description}</p> : null}
        </div>
      </header>

      <div className={styles.toolbar}>
        <button type="button" className={styles.iconButton} aria-label="新建笔记" title="新建笔记">
          <FilePlusIcon />
        </button>
        <button type="button" className={styles.iconButton} aria-label="查询" title="查询">
          <SearchIcon />
        </button>
      </div>

      <div className={styles.empty}>
        <p>知识库里什么也没有</p>
      </div>
    </section>
  )
}
