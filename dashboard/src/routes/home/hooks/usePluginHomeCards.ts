import { useCallback, useEffect, useState } from 'react'

import { getPluginHomeCards, type PluginHomeCard } from '@/lib/plugin-api'

export function usePluginHomeCards() {
  const [pluginHomeCards, setPluginHomeCards] = useState<PluginHomeCard[]>([])
  const [isPluginHomeCardsLoading, setIsPluginHomeCardsLoading] = useState(false)

  const fetchPluginHomeCards = useCallback(async () => {
    setIsPluginHomeCardsLoading(true)
    try {
      setPluginHomeCards(await getPluginHomeCards())
    } catch (error) {
      console.error('加载插件首页卡片失败:', error)
      setPluginHomeCards([])
    } finally {
      setIsPluginHomeCardsLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchPluginHomeCards()
  }, [fetchPluginHomeCards])

  return {
    pluginHomeCards,
    isPluginHomeCardsLoading,
    fetchPluginHomeCards,
  }
}
