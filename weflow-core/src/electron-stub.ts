/**
 * Electron API 桩模块
 * 替代 weflow-core 中所有 Electron 依赖，
 * 提供最小化的兼容接口。
 */
import { homedir, tmpdir } from 'os'
import { join } from 'path'

// --- app 桩 ---
export const app = {
  isPackaged: false,
  getPath(name: string): string {
    switch (name) {
      case 'userData': return process.env.WEFLOW_DATA_DIR || join(homedir(), '.weflow-core')
      case 'documents': return join(homedir(), 'Documents')
      case 'temp': return tmpdir()
      default: return homedir()
    }
  },
  getAppPath(): string {
    // 返回 weflow-core 根目录
    return process.env.WEFLOW_APP_PATH || join(__dirname, '..')
  },
  getVersion(): string {
    return '4.3.0-headless'
  },
  getName(): string {
    return 'weflow-core'
  },
}

// --- BrowserWindow 桩 ---
export const BrowserWindow = {
  getAllWindows(): any[] { return [] },
  getFocusedWindow(): any { return null },
  fromWebContents(): any { return null },
}

// --- dialog 桩 ---
export const dialog = {
  async showOpenDialog(): Promise<{ canceled: boolean; filePaths: string[] }> {
    return { canceled: true, filePaths: [] }
  },
  async showSaveDialog(): Promise<{ canceled: boolean; filePath?: string }> {
    return { canceled: true }
  },
  async showMessageBox(): Promise<{ response: number }> {
    return { response: 0 }
  },
}

// --- ipcMain 桩 ---
export const ipcMain = {
  handle(_channel: string, _handler: (...args: any[]) => any): void {},
  on(_channel: string, _handler: (...args: any[]) => any): void {},
  removeHandler(_channel: string): void {},
}

// --- ipcRenderer 桩 ---
export const ipcRenderer = {
  invoke(_channel: string, ..._args: any[]): Promise<any> { return Promise.resolve() },
  on(_channel: string, _handler: (...args: any[]) => any): void {},
  send(_channel: string, ..._args: any[]): void {},
}

// --- safeStorage 桩 (不做加密，明文存储) ---
export const safeStorage = {
  isEncryptionAvailable(): boolean { return false },
  encryptString(plaintext: string): Buffer { return Buffer.from(plaintext, 'utf8') },
  decryptString(buf: Buffer): string { return buf.toString('utf8') },
}

// --- shell 桩 ---
export const shell = {
  openPath(_path: string): Promise<string> { return Promise.resolve('') },
  openExternal(_url: string): Promise<void> { return Promise.resolve() },
}

// --- nativeTheme 桩 ---
export const nativeTheme = {
  shouldUseDarkColors: false,
  themeSource: 'system' as const,
}

// --- net 桩 ---
export const net = {
  fetch: undefined,
}

// --- Menu / Tray 桩 ---
export const Menu = { buildFromTemplate: () => ({}) }
export const Tray = function() {}
export const nativeImage = { createFromPath: () => ({}) }
export const Notification = function() {}
