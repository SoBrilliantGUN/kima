import { useRef, useState, type ChangeEvent, type DragEvent } from 'react'

import { ApiError } from '@/api/client'
import { CloseIcon } from '@/components/icons'
import { Modal } from '@/components/Modal'
import { useUploadDocument } from '@/hooks/useDocuments'
import styles from './index.module.scss'

type UploadStatus = 'pending' | 'uploading' | 'done' | 'error'

interface UploadItem {
  id: string
  name: string
  file: File
  status: UploadStatus
  error?: string
}

interface DocumentDropzoneProps {
  kbId: string
  onClose: () => void
}

const STATUS_LABEL: Record<UploadStatus, string> = {
  pending: '待上传',
  uploading: '上传中…',
  done: '已完成',
  error: '失败',
}

function isSupported(name: string): boolean {
  return /\.(pdf|docx)$/i.test(name)
}

export function DocumentDropzone({ kbId, onClose }: DocumentDropzoneProps) {
  const upload = useUploadDocument()
  const [items, setItems] = useState<UploadItem[]>([])
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  // 自增 id 计数器：给每个上传项生成稳定 key（避免用文件名/索引当 key）
  const idRef = useRef(0)

  // 按 id 更新单个上传项（用函数式 setState，避免闭包拿到旧 items）
  function patchItem(id: string, patch: Partial<UploadItem>) {
    setItems((prev) => prev.map((item) => (item.id === id ? { ...item, ...patch } : item)))
  }

  // 串行上传一批文件（逐个 await，避免并发压垮后端）：
  // 每项先置 uploading，成功后 done，失败则记下错误信息展示在列表里。
  async function uploadSequentially(batch: UploadItem[]) {
    for (const item of batch) {
      patchItem(item.id, { status: 'uploading' })
      try {
        await upload.mutateAsync({ kbId, file: item.file })
        patchItem(item.id, { status: 'done' })
      } catch (err) {
        patchItem(item.id, {
          status: 'error',
          error: err instanceof ApiError ? err.message : '上传失败，请重试',
        })
      }
    }
  }

  // 统一入口：拖拽和文件选择都走到这里。过滤掉不支持的扩展名，
  // 合法文件转成 pending 状态的上传项追加进列表，再触发串行上传。
  function addFiles(files: FileList | File[]) {
    const valid = Array.from(files).filter((file) => isSupported(file.name))
    if (valid.length === 0) return
    const batch: UploadItem[] = valid.map((file) => ({
      id: `file-${idRef.current++}`,
      name: file.name,
      file,
      status: 'pending',
    }))
    setItems((prev) => [...prev, ...batch])
    void uploadSequentially(batch)
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragOver(false)
    addFiles(event.dataTransfer.files)
  }

  // 清空 value，保证下次再选同一个文件也能再次触发 onChange
  function handleInputChange(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) addFiles(event.target.files)
    event.target.value = ''
  }

  return (
    <Modal onClose={onClose} className={styles.modal}>
      <header className={styles.header}>
        <h3 className={styles.title}>添加本地文档</h3>
        <button type="button" className={styles.close} onClick={onClose} aria-label="关闭">
          <CloseIcon />
        </button>
      </header>

      <div
        className={dragOver ? `${styles.dropzone} ${styles.dropzoneActive}` : styles.dropzone}
        onDragOver={(event) => {
          // 必须 preventDefault，否则浏览器默认行为会拦截 drop 事件
          event.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
      >
        <p className={styles.dropzoneTitle}>拖拽 PDF / Word 文档到此处</p>
        <p className={styles.dropzoneHint}>或点击选择文件（支持 .pdf / .docx，可多选）</p>
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.docx"
          multiple
          hidden
          onChange={handleInputChange}
        />
      </div>

      {items.length > 0 ? (
        <ul className={styles.list}>
          {items.map((item) => (
            <li key={item.id} className={styles.item}>
              <span className={styles.itemName}>{item.name}</span>
              <span className={`${styles.status} ${styles[item.status]}`}>
                {item.error ?? STATUS_LABEL[item.status]}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </Modal>
  )
}
