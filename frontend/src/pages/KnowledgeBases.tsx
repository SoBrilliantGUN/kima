import { useHealth } from '../hooks/useHealth'

export default function KnowledgeBases() {
  const { data, isError, isLoading } = useHealth()

  return (
    <div>
      <h2>知识库</h2>
      <p>占位页（模块 2 实现）</p>
      <p>
        Health:{' '}
        {isLoading ? 'loading…' : isError || !data ? 'unreachable' : `${data.status}/${data.database}`}
      </p>
    </div>
  )
}
