/**
 * MaiBot Dashboard 版本管理
 */

declare const __APP_VERSION__: string

export const APP_VERSION = __APP_VERSION__
export const APP_NAME = 'MaiBot Dashboard'
export const APP_FULL_NAME = `${APP_NAME} ${APP_VERSION}`

/**
 * 获取版本信息
 */
export const getVersionInfo = () => ({
  version: APP_VERSION,
  name: APP_NAME,
  fullName: APP_FULL_NAME,
  buildDate: import.meta.env.VITE_BUILD_DATE || new Date().toISOString().split('T')[0],
  buildEnv: import.meta.env.MODE,
})

/**
 * 格式化版本显示
 */
export const formatVersion = (prefix = 'v') => `${prefix}${APP_VERSION}`
