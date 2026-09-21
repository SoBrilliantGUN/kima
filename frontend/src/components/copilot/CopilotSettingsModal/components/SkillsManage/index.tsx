import { useQuery } from '@tanstack/react-query'

import { getCopilotSkills } from '@/api/copilot'

import styles from './index.module.scss'

/** Skills 管理：2 列等大技能卡片。 */
export function SkillsManage() {
  const { data } = useQuery({ queryKey: ['copilot-skills'], queryFn: getCopilotSkills })

  return (
    <div className={styles.manage}>
      <div className={styles.title}>Skills管理</div>
      <div className={styles.grid}>
        {(data?.items ?? []).map((skill) => (
          <div key={skill.name} className={styles.skillCard}>
            <div className={styles.skillName}>
              {skill.name}
              {skill.has_side_effect ? <em className={styles.write}>写</em> : null}
            </div>
            <div className={styles.skillDesc}>{skill.description}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
