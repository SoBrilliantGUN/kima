import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

import styles from './index.module.scss'

interface NavItem {
  to: string
  label: string
  icon: ReactNode
  end?: boolean
}

const items: NavItem[] = [
  {
    to: '/',
    label: 'kima',
    end: true,
    icon: (
      <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
    ),
  },
  {
    to: '/knowledge-bases',
    label: '知识库',
    icon: (
      <>
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
      </>
    ),
  },
  {
    to: '/notes',
    label: '笔记',
    icon: (
      <>
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <path d="M14 2v6h6" />
        <path d="M16 13H8" />
        <path d="M16 17H8" />
      </>
    ),
  },
  {
    to: '/browse',
    label: '浏览',
    icon: (
      <>
        <circle cx="12" cy="12" r="10" />
        <path d="M2 12h20" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </>
    ),
  },
]

export function Sidebar() {
  return (
    <nav className={styles.sidebar}>
      <div className={styles.brand}>K</div>
      <ul className={styles.nav}>
        {items.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              title={item.label}
              end={item.end}
              className={({ isActive }) => (isActive ? styles.active : undefined)}
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                {item.icon}
              </svg>
              <span>{item.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
