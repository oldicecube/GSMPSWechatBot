import type { PluginLoadProgress } from '@/lib/plugin-api/types'

import { unifiedWsClient } from './unified-ws'

type ProgressListener = (progress: PluginLoadProgress) => void

class PluginProgressClient {
  private initialized = false
  private listeners: Set<ProgressListener> = new Set()
  private subscriptionActive = false

  private initialize(): void {
    if (this.initialized) {
      return
    }

    unifiedWsClient.addEventListener((message) => {
      if (message.domain !== 'plugin_progress') {
        return
      }

      const progress = message.data.progress as PluginLoadProgress | undefined
      if (!progress) {
        return
      }

      this.listeners.forEach((listener) => {
        try {
          listener(progress)
        } catch (error) {
          console.error('插件进度监听器执行失败:', error)
        }
      })
    })

    this.initialized = true
  }

  async subscribe(listener: ProgressListener): Promise<() => Promise<void>> {
    this.initialize()
    this.listeners.add(listener)

    if (!this.subscriptionActive) {
      await unifiedWsClient.subscribe('plugin_progress', 'main')
      this.subscriptionActive = true
    }

    return async () => {
      this.listeners.delete(listener)
      if (this.listeners.size === 0 && this.subscriptionActive) {
        this.subscriptionActive = false
        await unifiedWsClient.unsubscribe('plugin_progress', 'main')
      }
    }
  }
}

export const pluginProgressClient = new PluginProgressClient()
