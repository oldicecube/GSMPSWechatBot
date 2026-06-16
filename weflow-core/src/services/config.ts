/**
 * 配置服务（无 Electron 依赖版本）
 * 替代 electron-store，使用 JSON 文件。
 * 对外保持 ConfigService 单例 API 兼容。
 */
import { join, dirname } from 'path'
import { existsSync, readdirSync, statSync, readFileSync, writeFileSync, mkdirSync } from 'fs'
import { homedir } from 'os'
import { expandHomePath } from '../utils/pathUtils'

// ==================== 类型 ====================

export interface WxidConfig {
  decryptKey?: string
  imageXorKey?: number
  imageAesKey?: string
  updatedAt?: number
}

interface CoreConfigData {
  dbPath: string
  decryptKey: string
  myWxid: string
  wxidConfigs: Record<string, WxidConfig>
  apiPort: number
  apiHost: string
  apiToken: string
  apiEnabled: boolean
  messagePushEnabled: boolean
  messagePushFilterMode: string
  messagePushFilterList: string[]
  resourcesPath: string
  theme: string
  language: string
  logEnabled: boolean
  exportPath: string
  cachePath: string
  lastOpenedDb: string
  lastSession: string
  onboardingDone: boolean
  imageXorKey: number
  imageAesKey: string
  authEnabled: boolean
  authPassword: string
  notificationEnabled: boolean
  notificationFilterMode: string
  notificationFilterList: string[]
  ignoredUpdateVersion: string
  updateChannel: string
  exportDefaultConcurrency: number
  analyticsExcludedUsernames: string[]
}

const DEFAULT_CONFIG: CoreConfigData = {
  dbPath: '',
  decryptKey: '',
  myWxid: '',
  wxidConfigs: {},
  apiPort: 5031,
  apiHost: '127.0.0.1',
  apiToken: '',
  apiEnabled: true,
  messagePushEnabled: true,
  messagePushFilterMode: 'all',
  messagePushFilterList: [],
  resourcesPath: '',
  theme: 'system',
  language: 'zh-CN',
  logEnabled: false,
  exportPath: '',
  cachePath: '',
  lastOpenedDb: '',
  lastSession: '',
  onboardingDone: false,
  imageXorKey: 0,
  imageAesKey: '',
  authEnabled: false,
  authPassword: '',
  notificationEnabled: true,
  notificationFilterMode: 'all',
  notificationFilterList: [],
  ignoredUpdateVersion: '',
  updateChannel: 'stable',
  exportDefaultConcurrency: 4,
  analyticsExcludedUsernames: [],
}

// ==================== ConfigService ====================

export class ConfigService {
  static instance: ConfigService | null = null
  configPath: string
  data: CoreConfigData
  initialized: boolean = false
  accountDirCache: Map<string, string> = new Map()

  constructor(configPath?: string) {
    if (ConfigService.instance) {
      return ConfigService.instance
    }
    ConfigService.instance = this

    this.configPath = configPath
      || process.env.WEFLOW_CONFIG_PATH
      || join(process.cwd(), 'config.json')
  }

  static getInstance(configPath?: string): ConfigService {
    if (!ConfigService.instance) {
      ConfigService.instance = new ConfigService(configPath)
    }
    return ConfigService.instance
  }

  static resetInstance(): void {
    ConfigService.instance = null
  }

  // ==================== 初始化 ====================

  init(): void {
    if (this.initialized) return
    this.data = this.load()
    this.initialized = true
  }

  // ==================== 文件 I/O ====================

  private load(): CoreConfigData {
    try {
      if (existsSync(this.configPath)) {
        const raw = readFileSync(this.configPath, 'utf8')
        const parsed = JSON.parse(raw)
        return this.normalize(parsed)
      }
    } catch (e) {
      console.error('[ConfigService] Load failed:', e)
    }
    return { ...DEFAULT_CONFIG }
  }

  save(): void {
    try {
      const dir = dirname(this.configPath)
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true })

      // ── 保留原始 JSON 结构：读取现有文件，更新 weflow 段 ──
      let raw: any = {}
      try {
        if (existsSync(this.configPath)) {
          raw = JSON.parse(readFileSync(this.configPath, 'utf8'))
        }
      } catch { /* 文件不存在或损坏，使用空对象 */ }

      if (!raw || typeof raw !== 'object') raw = {}

      // 确保 weflow 子对象存在
      if (!raw.weflow || typeof raw.weflow !== 'object') {
        raw.weflow = {}
      }

      // 将 weflow 相关字段写回 weflow 子对象
      const wf = raw.weflow
      wf.dbPath = this.data.dbPath
      wf.decryptKey = this.data.decryptKey
      wf.myWxid = this.data.myWxid
      wf.wxidConfigs = this.data.wxidConfigs
      wf.apiPort = this.data.apiPort
      wf.apiHost = this.data.apiHost
      wf.apiToken = this.data.apiToken
      wf.resourcesPath = this.data.resourcesPath
      wf.messagePushEnabled = this.data.messagePushEnabled
      wf.messagePushFilterMode = this.data.messagePushFilterMode
      wf.messagePushFilterList = this.data.messagePushFilterList

      // ── 清理旧占位 wxid（your-wxid-here），保留真实账号 ──
      if (wf.wxidConfigs && typeof wf.wxidConfigs === 'object') {
        const realWxid = this.data.myWxid
        for (const key of Object.keys(wf.wxidConfigs)) {
          if (key === 'your-wxid-here' && realWxid && wf.wxidConfigs[realWxid]) {
            delete wf.wxidConfigs[key]
          }
        }
      }

      // 仅同步 token 到顶层（Python 端读取 config.token）
      raw.token = this.data.apiToken
      raw.onboardingDone = this.data.onboardingDone

      // 删除可能残留的冗余顶层字段（已合并到 weflow 中）
      delete raw.dbPath
      delete raw.decryptKey
      delete raw.myWxid
      delete raw.imageXorKey
      delete raw.imageAesKey

      writeFileSync(this.configPath, JSON.stringify(raw, null, 2), 'utf8')
    } catch (e) {
      console.error('[ConfigService] Save failed:', e)
    }
  }

  private normalize(raw: any): CoreConfigData {
    if (!raw || typeof raw !== 'object') return { ...DEFAULT_CONFIG }
    const wf = (raw.weflow && typeof raw.weflow === 'object') ? raw.weflow : raw

    return {
      dbPath: String(wf.dbPath || raw.dbPath || DEFAULT_CONFIG.dbPath),
      decryptKey: String(wf.decryptKey || raw.decryptKey || ''),
      myWxid: String(wf.myWxid || raw.myWxid || ''),
      wxidConfigs: (wf.wxidConfigs && typeof wf.wxidConfigs === 'object') ? wf.wxidConfigs : {},
      apiPort: Number(wf.apiPort || raw.apiPort) || DEFAULT_CONFIG.apiPort,
      apiHost: String(wf.apiHost || raw.apiHost || DEFAULT_CONFIG.apiHost),
      apiToken: String(wf.apiToken || raw.token || raw.apiToken || ''),
      apiEnabled: true,
      messagePushEnabled: wf.messagePushEnabled !== false,
      messagePushFilterMode: String(wf.messagePushFilterMode || 'all'),
      messagePushFilterList: Array.isArray(wf.messagePushFilterList) ? wf.messagePushFilterList : [],
      resourcesPath: String(wf.resourcesPath || raw.resourcesPath || DEFAULT_CONFIG.resourcesPath),
      theme: String(raw.theme || DEFAULT_CONFIG.theme),
      language: String(raw.language || DEFAULT_CONFIG.language),
      logEnabled: Boolean(raw.logEnabled || wf.logEnabled),
      exportPath: String(raw.exportPath || ''),
      cachePath: String(raw.cachePath || ''),
      lastOpenedDb: String(raw.lastOpenedDb || ''),
      lastSession: String(raw.lastSession || ''),
      onboardingDone: Boolean(raw.onboardingDone || wf.onboardingDone),
      imageXorKey: Number(raw.imageXorKey || wf.imageXorKey) || 0,
      imageAesKey: String(raw.imageAesKey || wf.imageAesKey || ''),
      authEnabled: Boolean(raw.authEnabled),
      authPassword: String(raw.authPassword || ''),
      notificationEnabled: raw.notificationEnabled !== false,
      notificationFilterMode: String(raw.notificationFilterMode || 'all'),
      notificationFilterList: Array.isArray(raw.notificationFilterList) ? raw.notificationFilterList : [],
      ignoredUpdateVersion: String(raw.ignoredUpdateVersion || ''),
      updateChannel: String(raw.updateChannel || 'stable'),
      exportDefaultConcurrency: Number(raw.exportDefaultConcurrency) || 4,
      analyticsExcludedUsernames: Array.isArray(raw.analyticsExcludedUsernames) ? raw.analyticsExcludedUsernames : [],
    }
  }

  // ==================== get/set（内联初始化检查）====================

  get(key: string): any {
    if (!this.initialized) this.init()
    if (key === 'httpApiToken') return this.data.apiToken
    if (key === 'httpApiEnabled') return this.data.apiEnabled
    if (key === 'httpApiPort') return this.data.apiPort
    if (key === 'httpApiHost') return this.data.apiHost
    return (this.data as any)[key]
  }

  set(key: string, value: any): void {
    if (!this.initialized) this.init()
    if (key === 'httpApiToken') this.data.apiToken = value
    else if (key === 'httpApiPort') this.data.apiPort = value
    else if (key === 'httpApiHost') this.data.apiHost = value
    else (this.data as any)[key] = value
    this.save()
  }

  // ==================== 账号目录解析（简化但兼容）====================

  private isDirectory(p: string): boolean {
    try { return statSync(p).isDirectory() } catch { return false }
  }

  getAccountDir(dbPath?: string, wxid?: string): string | null {
    const actualDbPath = dbPath || this.data.dbPath
    const actualWxid = wxid || this.data.myWxid

    if (!actualDbPath || !actualWxid) return null

    const normalized = actualDbPath.replace(/[\\/]+$/, '')
    const lowerWxid = actualWxid.toLowerCase()
    const cacheKey = `${normalized}|${lowerWxid}`

    const cached = this.accountDirCache.get(cacheKey)
    if (cached && existsSync(cached)) return cached

    try {
      const entries = readdirSync(normalized)
      const candidates: Array<{ path: string; hasSession: boolean; mtime: number }> = []

      for (const entry of entries) {
        const entryPath = join(normalized, entry)
        if (!this.isDirectory(entryPath)) continue

        const lowerEntry = entry.toLowerCase()
        if (!(lowerEntry === lowerWxid || lowerEntry.startsWith(lowerWxid + '_'))) continue

        if (
          !existsSync(join(entryPath, 'db_storage')) &&
          !existsSync(join(entryPath, 'FileStorage', 'Image'))
        ) continue

        let mtime = 0
        try { mtime = statSync(entryPath).mtimeMs } catch { /* ignore */ }

        const hasSession = existsSync(join(entryPath, 'db_storage', 'session', 'session.db'))
          || existsSync(join(entryPath, 'db_storage', 'session.db'))

        candidates.push({ path: entryPath, hasSession, mtime })
      }

      if (candidates.length > 0) {
        candidates.sort((a, b) => {
          if (a.hasSession !== b.hasSession) return a.hasSession ? -1 : 1
          return b.mtime - a.mtime
        })
        const best = candidates[0].path
        this.accountDirCache.set(cacheKey, best)
        return best
      }
    } catch { }

    return null
  }

  // ==================== 路径 ====================

  getUserDataPath(): string {
    const envPath = process.env.WEFLOW_DATA_DIR
    if (envPath) return envPath
    return join(homedir(), '.weflow-core')
  }

  getCacheBasePath(): string {
    return join(this.getUserDataPath(), 'cache')
  }

  cleanAccountDirName(dirName: string): string {
    const trimmed = (dirName || '').trim()
    if (!trimmed) return trimmed
    if (trimmed.toLowerCase().startsWith('wxid_')) {
      const match = trimmed.match(/^(wxid_[^_]+)/i)
      if (match) return match[1]
      return trimmed
    }
    const suffixMatch = trimmed.match(/^(.+)_([a-zA-Z0-9]{4})$/)
    if (suffixMatch) return suffixMatch[1]
    return trimmed
  }

  getMyWxidCleaned(): string {
    const wxid = this.data.myWxid
    return wxid ? this.cleanAccountDirName(wxid) : ''
  }

  getImageKeysForCurrentWxid(): { xorKey: unknown; aesKey: string } {
    const wxid = this.data.myWxid
    if (wxid) {
      const cfg = this.data.wxidConfigs[wxid]
      if (cfg) {
        return {
          xorKey: cfg.imageXorKey ?? 0,
          aesKey: cfg.imageAesKey || this.data.imageAesKey || ''
        }
      }
    }
    return { xorKey: this.data.imageXorKey || 0, aesKey: this.data.imageAesKey || '' }
  }

  getAll(): Partial<CoreConfigData> {
    if (!this.initialized) this.init()
    return { ...this.data }
  }

  clear(): void {
    this.data = { ...DEFAULT_CONFIG }
    this.save()
  }
}
