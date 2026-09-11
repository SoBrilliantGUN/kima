import { NavLink } from 'react-router-dom'

import styles from './Sidebar.module.scss'

const items = [
  { to: '/knowledge-bases', label: '知识库' },
  { to: '/notes', label: '笔记' },
  { to: '/search', label: '搜索' },
  { to: '/chat', label: '问答' },
]

export function Sidebar() {
  return (
    <nav className={styles.sidebar}>
      <div className={styles.brand}>kima</div>
      <ul className={styles.nav}>
        {items.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              className={({ isActive }) => (isActive ? styles.active : undefined)}
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
