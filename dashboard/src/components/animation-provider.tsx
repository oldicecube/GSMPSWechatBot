import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { AnimationContext } from '@/lib/animation-context'

type AnimationProviderProps = {
  children: ReactNode
  defaultEnabled?: boolean
  storageKey?: string
}

export function AnimationProvider({
  children,
  defaultEnabled = true,
  storageKey = 'enable-animations',
}: AnimationProviderProps) {
  const [enableAnimations, setEnableAnimations] = useState<boolean>(() => {
    const stored = localStorage.getItem(storageKey)
    return stored !== null ? stored === 'true' : defaultEnabled
  })

  useEffect(() => {
    const root = document.documentElement

    if (enableAnimations) {
      root.classList.remove('no-animations')
    } else {
      root.classList.add('no-animations')
    }

    localStorage.setItem(storageKey, String(enableAnimations))
  }, [enableAnimations, storageKey])

  const value = {
    enableAnimations,
    setEnableAnimations,
  }

  return <AnimationContext value={value}>{children}</AnimationContext>
}
