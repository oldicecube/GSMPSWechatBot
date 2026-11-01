import * as React from 'react'

import { cn } from '@/lib/utils'

interface AccentPanelProps extends React.HTMLAttributes<HTMLDivElement> {
  contentClassName?: string
  showRetroStripes?: boolean
  showRetroStripeDivider?: boolean
}

const AccentPanel = React.forwardRef<HTMLDivElement, AccentPanelProps>(
  (
    {
      className,
      contentClassName,
      showRetroStripes = true,
      showRetroStripeDivider = true,
      children,
      ...props
    },
    ref
  ) => (
    <div
      ref={ref}
      data-dashboard-accent-panel="true"
      data-retro-stripes={showRetroStripes ? 'true' : 'false'}
      data-retro-stripe-divider={showRetroStripeDivider ? 'true' : 'false'}
      className={cn(
        'bg-card border-border text-card-foreground relative overflow-hidden border-2',
        className
      )}
      {...props}
    >
      <div
        aria-hidden="true"
        data-dashboard-accent-panel-stripes="true"
        className="border-border pointer-events-none absolute inset-y-0 left-0 hidden w-3 border-r-2"
      >
        <span className="w-1 bg-red-500" />
        <span className="w-1 bg-yellow-400" />
        <span className="w-1 bg-blue-500" />
      </div>
      <div
        data-dashboard-accent-panel-content="true"
        className={cn('relative min-h-full', contentClassName)}
      >
        {children}
      </div>
    </div>
  )
)
AccentPanel.displayName = 'AccentPanel'

export { AccentPanel }
