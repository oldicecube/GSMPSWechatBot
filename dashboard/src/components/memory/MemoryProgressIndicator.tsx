import { Loader2 } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'

export interface MemoryProgressIndicatorProps {
  /** 0-100 之间的进度百分比 */
  value: number
  /** 任务状态文本（如 “运行中”、“已完成”） */
  statusLabel?: string
  /** 当前步骤文本（如 “分块中”） */
  stepLabel?: string
  /** 状态对应的语义色（用于左侧圆环和徽标） */
  tone?: 'default' | 'success' | 'warning' | 'destructive' | 'muted'
  /** 是否显示加载动画（运行中/取消中场景） */
  busy?: boolean
  /** 紧凑模式：用于队列列表项 */
  compact?: boolean
  /** 额外说明（如 “已完成 36 / 120 分块”） */
  detail?: string
  className?: string
}

const TONE_RING_CLASS: Record<NonNullable<MemoryProgressIndicatorProps['tone']>, string> = {
  default: 'text-primary',
  success: 'text-emerald-500',
  warning: 'text-amber-500',
  destructive: 'text-rose-500',
  muted: 'text-muted-foreground',
}

const TONE_BADGE_VARIANT: Record<
  NonNullable<MemoryProgressIndicatorProps['tone']>,
  'default' | 'secondary' | 'destructive' | 'outline'
> = {
  default: 'default',
  success: 'secondary',
  warning: 'outline',
  destructive: 'destructive',
  muted: 'outline',
}

/**
 * 长期记忆控制台统一的任务进度展示组件。
 *
 * 设计目标：
 *  - 让用户一眼看清「整体百分比 + 语义状态 + 当前步骤」。
 *  - 复用 shadcn `Progress` 与 `Badge`，避免引入额外样式来源。
 *  - 在紧凑模式下保留可读性，可放进队列卡片；非紧凑模式带圆环用于详情区。
 */
export function MemoryProgressIndicator({
  value,
  statusLabel,
  stepLabel,
  tone = 'default',
  busy = false,
  compact = false,
  detail,
  className,
}: MemoryProgressIndicatorProps) {
  const safeValue = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0
  const ringSize = compact ? 36 : 56
  const ringStroke = compact ? 4 : 5
  const radius = (ringSize - ringStroke) / 2
  const circumference = 2 * Math.PI * radius
  const dashOffset = circumference * (1 - safeValue / 100)

  return (
    <div className={cn('flex items-center gap-3', className)}>
      <div
        className={cn('relative shrink-0', TONE_RING_CLASS[tone])}
        style={{ width: ringSize, height: ringSize }}
        aria-hidden="true"
      >
        <svg width={ringSize} height={ringSize} className="-rotate-90">
          <circle
            cx={ringSize / 2}
            cy={ringSize / 2}
            r={radius}
            strokeWidth={ringStroke}
            className="stroke-muted/40"
            fill="none"
          />
          <circle
            cx={ringSize / 2}
            cy={ringSize / 2}
            r={radius}
            strokeWidth={ringStroke}
            strokeLinecap="round"
            stroke="currentColor"
            fill="none"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
            className="transition-[stroke-dashoffset] duration-500 ease-out"
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          {busy ? (
            <Loader2 className={cn('animate-spin', compact ? 'h-3.5 w-3.5' : 'h-4 w-4')} />
          ) : (
            <span className={cn('font-medium tabular-nums', compact ? 'text-[10px]' : 'text-xs')}>
              {Math.round(safeValue)}%
            </span>
          )}
        </div>
      </div>

      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          {statusLabel ? (
            <Badge variant={TONE_BADGE_VARIANT[tone]} className="shrink-0">
              {statusLabel}
            </Badge>
          ) : null}
          {stepLabel ? (
            <span className="truncate text-xs text-muted-foreground">{stepLabel}</span>
          ) : null}
          {!compact ? (
            <span className="ml-auto text-xs tabular-nums text-muted-foreground">
              {safeValue.toFixed(1)}%
            </span>
          ) : null}
        </div>
        <Progress value={safeValue} className={cn(compact ? 'h-1' : 'h-1.5')} />
        {detail ? <div className="truncate text-xs text-muted-foreground">{detail}</div> : null}
      </div>
    </div>
  )
}
