import type { ComponentType } from 'react'
import { NavLink } from 'react-router-dom'

import { BookIcon, FileTextIcon, SparkleIcon, type IconProps } from '@/components/icons'
import styles from './index.module.scss'

interface NavItem {
  to: string
  label: string
  Icon: ComponentType<IconProps>
  end?: boolean
}

const items: NavItem[] = [
  { to: '/', label: 'kima', end: true, Icon: SparkleIcon },
  { to: '/knowledge-bases', label: '知识库', Icon: BookIcon },
  { to: '/notes', label: '笔记', Icon: FileTextIcon },
]

export function Sidebar() {
  return (
    <nav className={styles.sidebar}>
      <div className={styles.brand}>K</div>
      <ul className={styles.nav}>
        {items.map(({ to, label, end, Icon }) => (
          <li key={to}>
            <NavLink
              to={to}
              title={label}
              end={end}
              className={({ isActive }) => (isActive ? styles.active : undefined)}
            >
              <Icon strokeWidth={1.8} />
              <span>{label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
