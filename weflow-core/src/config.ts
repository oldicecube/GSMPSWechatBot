/**
 * weflow-core 配置服务
 * 使用 JSON 文件替代 electron-store
 */
import { join } from 'path'
import { existsSync, readdirSync, statSync, readFileSync, writeFileSync } from 'fs'
import { homedir } from 'os'

// --- 配置 Schema ---
export interface WxidConfig {
  decryptKey?: string
  imageXorKey?: number
  imageAesKey?: string
  updatedAt?: number
}

export interface CoreConfig {
  weflow: {
    dbPath: string
    decryptKey?: string
    myWxid?: string
    wxidConfigs?: Record<string, WxidConfig>
    apiPort: number
    apiHost: string
    resourcesPath: string
    messagePushEnabled?: boolean
    messagePushFilterMode?: string
    messagePushFilterList?: string[]
  }
  token?: string
  target_group?: string[]
  [key: string]: any
}

export class ConfigService {
  private static instance: ConfigService | null = null
  private configPath: string
  private data: CoreConfig

  constructor(configPath?: string) {
    if (ConfigService.instance) {
      return ConfigService.instance
    }
    ConfigService.instance = this

    this.configPath = configPath || process.env.WEFLOW_CONFIG_PATH || join(process.cwd(), 'config.json')
    this.data = this.load()
  }

  static getInstance(): ConfigService {
    if (!ConfigService.instance) {
      ConfigService.instance = new ConfigService()
    }
    return ConfigService.instance
  }

  // ==================== 文件读写 ====================

  private load(): CoreConfig {
    try {
      if (existsSync(this.configPath)) {
        const raw = readFileSync(this.configPath, 'utf8')
        const parsed = JSON.parse(raw)
        return this.normalizeConfig(parsed)
      }
    } catch (e) {
      console.error('[config] 加载配置失败:', e)
    }
    return this.defaults()
  }

  save(): void {
    try {
      writeFileSync(this.configPath, JSON.stringify(this.data, null, 2), 'utf8')
    } catch (e) {
      console.error('[config] 保存配置失败:', e)
    }
  }

  private defaults(): CoreConfig {
    return {
      weflow: {
        dbPath: '',
        decryptKey: '',
        myWxid: '',
        wxidConfigs: {},
        apiPort: 5031,
        apiHost: '127.0.0.1',
        resourcesPath: '',
        messagePushEnabled: true,
        messagePushFilterMode: 'all',
        messagePushFilterList: [],
      }
    }
  }

  private normalizeConfig(raw: any): CoreConfig {
    const defaults = this.defaults()
    if (!raw || typeof raw !== 'object') return defaults
    if (!raw.weflow || typeof raw.weflow !== 'object') {
      raw.weflow = defaults.weflow
    }

    const wf = raw.weflow
    return {
      ...raw,
      weflow: {
        dbPath: String(wf.dbPath || defaults.weflow.dbPath),
        decryptKey: String(wf.decryptKey || ''),
        myWxid: String(wf.myWxid || ''),
        wxidConfigs: wf.wxidConfigs && typeof wf.wxidConfigs === 'object' ? wf.wxidConfigs : {},
        apiPort: Number(wf.apiPort) || defaults.weflow.apiPort,
        apiHost: String(wf.apiHost || defaults.weflow.apiHost),
        resourcesPath: String(wf.resourcesPath || defaults.weflow.resourcesPath),
        messagePushEnabled: wf.messagePushEnabled !== false,
        messagePushFilterMode: String(wf.messagePushFilterMode || 'all'),
        messagePushFilterList: Array.isArray(wf.messagePushFilterList) ? wf.messagePushFilterList : [],
      }
    }
  }

  // ==================== 兼容旧 API ====================

  get(key: string): any {
    const wf = this.data?.weflow as any
    if (wf && key in wf && key !== 'dbPath' && key !== 'decryptKey') {
      return wf[key]
    }
    if (key === 'dbPath') return this.data.weflow.dbPath
    if (key === 'decryptKey') return this.data.weflow.decryptKey
    if (key === 'myWxid') return this.data.weflow.myWxid
    if (key === 'wxidConfigs') return this.data.weflow.wxidConfigs
    if (key === 'httpApiPort') return this.data.weflow.apiPort
    if (key === 'httpApiHost') return this.data.weflow.apiHost
    if (key === 'httpApiEnabled') return true
    if (key === 'messagePushEnabled') return this.data.weflow.messagePushEnabled !== false
    if (key === 'httpApiToken') return this.data.token || ''
    if (key === 'resourcesPath') return this.data.weflow.resourcesPath
    return undefined
  }

  set(key: string, value: any): void {
    const wf = this.data.weflow as any
    switch (key) {
      case 'dbPath': this.data.weflow.dbPath = String(value || ''); break
      case 'decryptKey': this.data.weflow.decryptKey = String(value || ''); break
      case 'myWxid': this.data.weflow.myWxid = String(value || ''); break
      case 'wxidConfigs': this.data.weflow.wxidConfigs = value || {}; break
      case 'httpApiPort': this.data.weflow.apiPort = Number(value) || 5031; break
      case 'httpApiHost': this.data.weflow.apiHost = String(value || '127.0.0.1'); break
      case 'messagePushEnabled': this.data.weflow.messagePushEnabled = value !== false; break
      case 'httpApiToken': this.data.token = String(value || ''); break
      default:
        if (wf && typeof wf === 'object') wf[key] = value
        break
    }
    this.save()
  }

  // ==================== 路径相关 ====================

  getAccountDir(dbPath?: string, wxid?: string): string | null {
    const actualDbPath = dbPath || this.data.weflow.dbPath
    const actualWxid = wxid || this.data.weflow.myWxid

    if (!actualDbPath || !actualWxid) return null

    try {
      const entries = readdirSync(actualDbPath)
      for (const entry of entries) {
        const entryPath = join(actualDbPath, entry)
        try {
          const st = statSync(entryPath)
          if (!st.isDirectory()) continue
        } catch { continue }

        if (entry.toLowerCase().startsWith(actualWxid.toLowerCase().replace(/_/g, '').substring(0, 6))) {
          // 模糊匹配 wxid
          return entryPath
        }
        if (entry.toLowerCase() === actualWxid.toLowerCase()) {
          return entryPath
        }
      }

      // 兜底：浏览所有子目录寻找包含该 wxid 的
      for (const entry of entries) {
        const entryPath = join(actualDbPath, entry)
        try {
          const st = statSync(entryPath)
          if (!st.isDirectory()) continue
        } catch { continue }
        if (entry.toLowerCase().includes('wxid_')) {
          // 检查是否有 db_storage
          const dbStorage = join(entryPath, 'db_storage')
          if (existsSync(dbStorage)) return entryPath
        }
      }
    } catch { }

    return null
  }

  getUserDataPath(): string {
    return join(homedir(), '.weflow-core')
  }

  getCacheBasePath(): string {
    return join(this.getUserDataPath(), 'cache')
  }
}

// 默认实例
let defaultInstance: ConfigService | null = null

export function getConfig(configPath?: string): ConfigService {
  if (!defaultInstance) {
    defaultInstance = new ConfigService(configPath)
  }
  return defaultInstance
}
