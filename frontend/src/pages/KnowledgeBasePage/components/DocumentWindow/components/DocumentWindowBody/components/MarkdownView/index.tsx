import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import styles from './index.module.scss'

interface MarkdownViewProps {
  markdown: string
}

// 只读 markdown 渲染：react-markdown + remark-gfm，原生支持 GFM 表格/图片/代码等。
// 不渲染原始 HTML（react-markdown 默认转义），比手写 Tiptap 更贴合只读场景。
export function MarkdownView({ markdown }: MarkdownViewProps) {
  return (
    <div className={styles.viewer}>
      <div className={styles.content}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
      </div>
    </div>
  )
}
