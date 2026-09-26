import { useQuery } from '@tanstack/react-query'

import { getCopilotCustomSkills, getCopilotSkills } from '@/api/copilot'

import styles from './index.module.scss'

/** Skills 管理：分「我安装的 skills」（自定义，MD 文件）与「官方内置的 skills」（工具目录）两区。 */
export function SkillsManage() {
  const builtin = useQuery({ queryKey: ['copilot-skills'], queryFn: getCopilotSkills })
  const custom = useQuery({
    queryKey: ['copilot-custom-skills'],
    queryFn: getCopilotCustomSkills,
  })

  const builtinItems = builtin.data?.items ?? []
  const customItems = custom.data?.items ?? []

  return (
    <div className={styles.manage}>
      <div className={styles.title}>Skills管理</div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>我安装的 skills</div>
        {customItems.length === 0 ? (
          <div className={styles.empty}>（暂无自定义 skill，往 data/skills 放 MD 文件即可）</div>
        ) : (
          <div className={styles.grid}>
            {customItems.map((skill) => (
              <div key={skill.name} className={styles.skillCard}>
                <div className={styles.skillName}>{skill.name}</div>
                <div className={styles.skillDesc}>{skill.description || skill.content}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>官方内置的 skills</div>
        <div className={styles.grid}>
          {builtinItems.map((skill) => (
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
    </div>
  )
}
