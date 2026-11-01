import { createContext } from 'react'

export type AnimationSettings = {
  enableAnimations: boolean
  setEnableAnimations: (enable: boolean) => void
}

export const AnimationContext = createContext<AnimationSettings | undefined>(undefined)
