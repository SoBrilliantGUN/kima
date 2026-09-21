import { useId } from 'react'

interface Props {
  size?: number
}

/** Copilot 头像：方形机械机器人脸（天线 + 镜头眼 + 扬声器格栅 + 角落螺栓）。 */
export function CopilotAvatar({ size = 40 }: Props) {
  const uid = useId()
  const baseId = `${uid}-base`

  return (
    <svg width={size} height={size} viewBox="0 0 48 48" aria-label="copilot头像">
      <defs>
        <linearGradient id={baseId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#3b5bdb" />
          <stop offset="100%" stopColor="#748ffc" />
        </linearGradient>
      </defs>

      {/* 天线 */}
      <path d="M24 2v4" stroke="#3b5bdb" strokeWidth="2" strokeLinecap="round" />
      <circle cx="24" cy="2.2" r="2.2" fill="#3b5bdb" />

      {/* 方形头 */}
      <rect x="4" y="7" width="40" height="38" rx="7" fill={`url(#${baseId})`} />

      {/* 内凹面板 */}
      <rect x="8.5" y="11.5" width="31" height="29" rx="4" fill="#ffffff" opacity="0.07" />

      {/* 镜头眼 */}
      <rect x="13" y="19" width="9" height="8" rx="2" fill="#ffffff" />
      <rect x="26" y="19" width="9" height="8" rx="2" fill="#ffffff" />
      <rect x="15.5" y="21.5" width="4" height="3" rx="1" fill="#3b5bdb" />
      <rect x="28.5" y="21.5" width="4" height="3" rx="1" fill="#3b5bdb" />

      {/* 扬声器格栅 */}
      <rect x="16" y="34" width="16" height="3" rx="1.5" fill="#ffffff" opacity="0.85" />

      {/* 角落螺栓 */}
      <circle cx="9" cy="12" r="1.4" fill="#ffffff" opacity="0.45" />
      <circle cx="39" cy="12" r="1.4" fill="#ffffff" opacity="0.45" />
      <circle cx="9" cy="40" r="1.4" fill="#ffffff" opacity="0.45" />
      <circle cx="39" cy="40" r="1.4" fill="#ffffff" opacity="0.45" />
    </svg>
  )
}
