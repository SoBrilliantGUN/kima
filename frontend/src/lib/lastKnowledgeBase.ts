// 记住上次选中的知识库 id，供 /knowledge-bases 重定向回退到它而非第一个
let lastActiveId: string | null = null

export function rememberKnowledgeBase(id: string): void {
  lastActiveId = id
}

export function recallKnowledgeBase(): string | null {
  return lastActiveId
}
