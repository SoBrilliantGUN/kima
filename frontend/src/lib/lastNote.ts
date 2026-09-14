// 记住上次打开的笔记 id，供 /notes 重定向回退到它而非空态
let lastActiveId: string | null = null

export function rememberNote(id: string): void {
  lastActiveId = id
}

export function recallNote(): string | null {
  return lastActiveId
}

export function forgetNote(): void {
  lastActiveId = null
}
