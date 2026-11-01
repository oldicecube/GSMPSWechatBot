import { createContext, useContext } from 'react'

/** 暴露给消息内容渲染器使用的滚动 / 高亮接口。 */
export interface ChatScrollContextValue {
  /** 滚动并高亮指定消息；若消息不在可视列表中则返回 ``false``。 */
  scrollToMessage: (messageId: string) => boolean
}

export const ChatScrollContext = createContext<ChatScrollContextValue | null>(null)

/** 在消息列表内部使用：访问 ``scrollToMessage`` 等能力。 */
export function useChatScroll(): ChatScrollContextValue | null {
  return useContext(ChatScrollContext)
}
