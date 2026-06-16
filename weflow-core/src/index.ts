/**
 * weflow-core CLI 入口
 * 无头模式：启动 WCDB 数据库服务 + HTTP API
 *
 * 用法: node dist/index.js [config.json路径]
 */

import * as path from 'path';
import * as readline from 'readline';
import { existsSync, mkdirSync, readdirSync, statSync } from 'fs';

// 初始化全局 Electron 桩（在 import 其他模块之前）
// 通过设置环境变量让 wcdbService 正确解析 Worker 路径
process.env.NODE_ENV = process.env.NODE_ENV || 'production';

const CONFIG_PATH = process.argv[2]
  || process.env.WEFLOW_CONFIG_PATH
  || path.join(process.cwd(), 'config.json');

console.log('[weflow-core] v4.3.0-headless');
console.log(`[weflow-core] 配置路径: ${CONFIG_PATH}`);

// ==================== 导入业务模块 ====================

// 确保数据目录存在
function ensureDataDir(basePath: string) {
  const dirs = [basePath, path.join(basePath, 'logs'), path.join(basePath, 'cache')];
  for (const d of dirs) {
    if (!existsSync(d)) {
      try { mkdirSync(d, { recursive: true }); } catch { /* ignore */ }
    }
  }
}

// ==================== 交互式配置引导 ====================

async function guidedSetup(configPath: string): Promise<boolean> {
  const { ConfigService } = require('./services/config');
  const { DbPathService, dbPathService } = require('./services/dbPathService');

  // ── 使用传入的 configPath，确保保存到正确的 config.json ──
  const config = ConfigService.getInstance(configPath);
  config.init();

  // ── 检查是否已完成新人引导 ──
  // 不仅检查 onboardingDone 标记，还要检查所有关键字段是否实际有效
  const onboardingDone = config.get('onboardingDone');
  const existingDbPath = config.get('dbPath');
  const existingDecryptKey = config.get('decryptKey');
  const existingMyWxid = config.get('myWxid');
  const wxidConfigs = config.get('wxidConfigs') || {};

  const hasValidDbPath = existingDbPath && existsSync(existingDbPath);
  const hasValidKey = existingDecryptKey && existingDecryptKey.length === 64;
  const hasValidWxid = existingMyWxid && existingMyWxid.startsWith('wxid_');

  if (onboardingDone && hasValidDbPath && hasValidKey && hasValidWxid) {
    console.log('[weflow-core] 使用已有配置 (onboardingDone=true)');
    return true;
  }

  // ── 即使 onboardingDone 未设置，如果所有关键字段都已有效，也跳过引导 ──
  if (!onboardingDone && hasValidDbPath && hasValidKey && hasValidWxid) {
    console.log('[weflow-core] 检测到完整配置，自动标记为已完成');
    config.set('onboardingDone', true);
    return true;
  }

  // ── 自动生成 API Token（内部鉴权用，无需用户手动填写）──
  const existingToken = config.get('httpApiToken') || '';
  if (!existingToken || existingToken === 'your-token-here') {
    const crypto = require('crypto');
    const newToken = crypto.randomBytes(32).toString('hex');
    config.set('httpApiToken', newToken);
    console.log(`[weflow-core] 已自动生成 API Token: ${newToken.slice(0, 8)}...`);
  }

  // ── 收集所有缺失/无效的配置项 ──
  const missingFields: string[] = [];
  if (!hasValidDbPath) {
    missingFields.push('weflow.dbPath');
  }
  if (!hasValidKey) {
    missingFields.push('weflow.decryptKey');
  }
  if (!hasValidWxid) {
    missingFields.push('weflow.myWxid');
  }

  // ── 非 TTY 环境（如被 Python 子进程启动时 stdin 是 PIPE）──
  // 跳过交互式引导，列出所有缺失字段，让用户手动编辑 config.json
  if (!process.stdin.isTTY) {
    console.log('\n╔══════════════════════════════════════════════╗');
    console.log('║  ⚠️  weflow-core 首次运行 — 需要完成配置     ║');
    console.log('╠══════════════════════════════════════════════╣');
    console.log('║                                              ║');
    console.log('║  当前配置缺失以下字段:                       ║');
    for (const field of missingFields) {
      const pad = field.padEnd(28);
      console.log(`║    ✗ ${pad} ║`);
    }
    console.log('║                                              ║');
    console.log('║  请在 config.json 的 weflow 段中填写:        ║');
    console.log('║                                              ║');
    console.log('║  {                                           ║');
    console.log('║    "weflow": {                               ║');
    console.log('║      "dbPath": "D:\\\\Documents\\\\xwechat_files", ║');
    console.log('║      "decryptKey": "<64位十六进制密钥>",     ║');
    console.log('║      "myWxid": "wxid_xxxxxxxxxxxxx"         ║');
    console.log('║    }                                         ║');
    console.log('║  }                                           ║');
    console.log('║                                              ║');
    console.log('║  💡 解密密钥获取方式:                        ║');
    console.log('║   1. 安装 WeFlow GUI 版自动提取              ║');
    console.log('║   2. 运行 weflow-core 在交互终端中自动获取   ║');
    console.log('║                                              ║');
    console.log('║  填完后重新运行即可。                        ║');
    console.log('╚══════════════════════════════════════════════╝\n');
    return false;
  }

  console.log('\n╔══════════════════════════════════╗');
  console.log('║   🔧 weflow-core 首次配置向导    ║');
  console.log('╚══════════════════════════════════╝\n');

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  const ask = (q: string): Promise<string> =>
    new Promise((resolve) => rl.question(q, resolve));

  try {
    // --- Step 1: dbPath ---
    const defaultPath = dbPathService ? dbPathService.getDefaultPath() : '';
    let dbPath: string;

    if (defaultPath && existsSync(defaultPath)) {
      console.log(`📁 自动探测到微信数据目录:`);
      console.log(`   ${defaultPath}\n`);
      const answer = await ask('   使用此路径？[Y/n] ');
      dbPath = answer.toLowerCase() === 'n'
        ? await ask('   请输入微信数据目录路径: ')
        : defaultPath;
    } else {
      console.log('⚠️  未能自动探测微信数据目录');
      console.log('   示例 Windows: C:\\Users\\用户名\\Documents\\WeChat Files\n');
      dbPath = await ask('   请输入微信数据目录路径: ');
    }

    if (!dbPath || !existsSync(dbPath)) {
      console.log(`❌ 路径不存在: ${dbPath}`);
      rl.close();
      return false;
    }

    config.set('dbPath', dbPath);

    // --- Step 2: 获取密钥（自动提取 或 手动输入） ---
    console.log('\n🔑 获取数据库密钥...');
    console.log('   [1] 自动从微信进程提取（推荐，需要微信已登录）');
    console.log('   [2] 手动输入 64 位十六进制密钥\n');

    const keyChoice = await ask('   请选择 [1]: ');
    let hexKey: string;

    if (keyChoice === '2') {
      hexKey = await ask('\n   请输入 64 位十六进制解密密钥: ');
      hexKey = hexKey.trim();
      if (!/^[0-9a-fA-F]{64}$/.test(hexKey)) {
        console.log('❌ 密钥格式无效（需要 64 位十六进制字符串）');
        rl.close();
        return false;
      }
      console.log('✅ 已使用手动输入的密钥');
    } else {
      console.log('\n   正在从微信进程提取密钥...');
      console.log('   请确保微信已启动并完成登录\n');

      const platform = process.platform;
      let keyService: any;

      if (platform === 'darwin') {
        const { KeyServiceMac } = require('./services/keyServiceMac');
        keyService = new KeyServiceMac();
      } else if (platform === 'linux') {
        const { KeyServiceLinux } = require('./services/keyServiceLinux');
        keyService = new KeyServiceLinux();
      } else {
        const { KeyService } = require('./services/keyService');
        keyService = new KeyService();
      }

      const result = await keyService.autoGetDbKey(
        120_000,
        (msg: string, _level: number) => {
          console.log(`   ${msg}`);
        }
      );

      if (!result.success) {
        console.log(`\n❌ 密钥提取失败: ${result.error}`);
        if (result.logs?.length) {
          console.log('\n   详细日志:');
          result.logs.forEach((l: string) => console.log(`     ${l}`));
        }
        console.log('\n💡 提示: 你也可以选择手动输入密钥，重新运行配置向导即可');
        rl.close();
        return false;
      }

      console.log('\n✅ 密钥提取成功');
      hexKey = result.key;
    }

    // 自动检测 wxid
    let wxid = await detectWxid(dbPath);
    if (wxid) {
      console.log(`📱 检测到账号: ${wxid}`);
    } else {
      // 自动检测失败，列出候选目录让用户选择
      console.log('\n⚠️  未能自动识别账号，正在扫描目录...');
      const manualCandidates = listWxidCandidates(dbPath);
      if (manualCandidates.length > 0) {
        console.log('\n📁 检测到以下候选账号目录:');
        manualCandidates.forEach((c, i) => {
          console.log(`   [${i + 1}] ${c}`);
        });
        console.log('   [0] 手动输入');
        const choice = await ask('\n   请选择账号 [1]: ');
        const idx = parseInt(choice) || 1;
        if (idx > 0 && idx <= manualCandidates.length) {
          wxid = manualCandidates[idx - 1];
        }
      }
      if (!wxid) {
        wxid = await ask('\n   请输入你的 wxid (例如 wxid_abc1234): ');
      }
    }

    if (!wxid || !wxid.trim()) {
      console.log('❌ wxid 不能为空');
      rl.close();
      return false;
    }
    wxid = wxid.trim();
    config.set('myWxid', wxid);

    // 保存密钥到 wxidConfigs
    const wxidConfigs = config.get('wxidConfigs') || {};
    if (wxid) {
      wxidConfigs[wxid] = {
        decryptKey: hexKey,
        updatedAt: Date.now(),
      };
    }
    config.set('decryptKey', hexKey);
    config.set('wxidConfigs', wxidConfigs);

    // ── 增量保存：在尝试图片密钥之前先保存核心配置 ──
    // 防止图片密钥 DLL 调用崩溃导致已获取的密钥丢失
    config.set('onboardingDone', true);
    console.log('   💾 核心配置已保存 (dbPath + decryptKey + wxid)');

    // --- Step 3: 获取图片密钥 (XOR + AES) ---
    console.log('\n🖼️  获取图片解密密钥 (可选，可跳过)...');
    console.log('   图片密钥用于解密微信聊天中的图片');

    const imgKeyChoice = await ask('\n   是否自动获取图片密钥？[Y/n] ');
    if (imgKeyChoice.toLowerCase() !== 'n') {
      try {
        const platform = process.platform;
        let keyService: any;

        if (platform === 'darwin') {
          const { KeyServiceMac } = require('./services/keyServiceMac');
          keyService = new KeyServiceMac();
        } else if (platform === 'linux') {
          const { KeyServiceLinux } = require('./services/keyServiceLinux');
          keyService = new KeyServiceLinux();
        } else {
          const { KeyService } = require('./services/keyService');
          keyService = new KeyService();
        }

        const accountDir = config.getAccountDir(dbPath, wxid) || path.join(dbPath, wxid);
        console.log('   正在提取图片密钥...');
        const imgResult = await keyService.autoGetImageKey(
          accountDir,
          (msg: string) => { console.log(`   ${msg}`); },
          wxid
        );

        if (imgResult.success) {
          const wxidConfigs = config.get('wxidConfigs') || {};
          wxidConfigs[wxid] = {
            ...(wxidConfigs[wxid] || {}),
            imageXorKey: imgResult.xorKey,
            imageAesKey: imgResult.aesKey,
            updatedAt: Date.now(),
            decryptKey: hexKey,
          };
          config.set('wxidConfigs', wxidConfigs);
          console.log(`✅ 图片密钥获取成功 (XOR=0x${imgResult.xorKey?.toString(16)}, AES=${imgResult.aesKey})`);
        } else {
          console.log(`⚠️  图片密钥获取失败: ${imgResult.error}`);
          console.log('   💡 可稍后在 WeFlow GUI 中设置，或手动填入 config.json');
        }
      } catch (e: any) {
        console.log(`⚠️  图片密钥提取异常: ${e.message}`);
        console.log('   💡 可稍后在 WeFlow GUI 中设置，或手动填入 config.json');
      }
    }

    // --- Step 4: 测试连接 ---
    console.log('\n🔗 测试数据库连接...');
    const accountDir = config.getAccountDir(dbPath, wxid);
    if (!accountDir) {
      console.log(`❌ 未找到账号目录 (dbPath=${dbPath}, wxid=${wxid})`);
      rl.close();
      return false;
    }

    const { wcdbService } = require('./services/wcdbService');
    // 设置资源路径
    const resourcesPath = path.join(__dirname, '..', 'resources');
    wcdbService.setPaths(resourcesPath, config.getUserDataPath());

    const testResult = await wcdbService.testConnection(accountDir, hexKey);
    if (!testResult.success) {
      console.log(`❌ 数据库连接失败: ${testResult.error}`);
      rl.close();
      return false;
    }
    console.log('✅ 数据库连接成功');

    config.set('onboardingDone', true);
    console.log('\n╔══════════════════════════════════╗');
    console.log('║   ✅ 配置完成！正在启动服务...   ║');
    console.log('╚══════════════════════════════════╝\n');

    return true;
  } finally {
    rl.close();
  }
}

async function detectWxid(dbPath: string): Promise<string | null> {
  // 直接扫描 dbPath 下包含 db_storage 的子目录，提取 wxid
  try {
    const entries = readdirSync(dbPath);
    const candidates: Array<{ wxid: string; mtime: number }> = [];

    for (const entry of entries) {
      const entryPath = path.join(dbPath, entry);
      try {
        const st = statSync(entryPath);
        if (!st.isDirectory()) continue;
      } catch { continue; }

      // 检查是否包含 db_storage（微信账号目录特征）
      if (
        existsSync(path.join(entryPath, 'db_storage')) ||
        existsSync(path.join(entryPath, 'FileStorage', 'Image'))
      ) {
        let mtime = 0;
        try { mtime = statSync(entryPath).mtimeMs; } catch { /* ignore */ }
        candidates.push({ wxid: entry, mtime });
      }
    }

    // 按修改时间降序排列，优先选最新的
    candidates.sort((a, b) => b.mtime - a.mtime);

    if (candidates.length > 0) {
      return candidates[0].wxid;
    }

    // 兜底：dbPath 本身可能就是账号目录
    const dbStorage = path.join(dbPath, 'db_storage');
    if (existsSync(dbStorage)) {
      return path.basename(dbPath);
    }
  } catch (e) {
    console.log(`   [debug] detectWxid 扫描异常: ${(e as Error).message}`);
  }
  return null;
}

/** 列出所有候选 wxid（用于手动选择） */
function listWxidCandidates(dbPath: string): string[] {
  try {
    const entries = readdirSync(dbPath);
    return entries.filter(entry => {
      const entryPath = path.join(dbPath, entry);
      try {
        const st = statSync(entryPath);
        if (!st.isDirectory()) return false;
      } catch { return false; }
      return (
        existsSync(path.join(entryPath, 'db_storage')) ||
        existsSync(path.join(entryPath, 'FileStorage', 'Image'))
      );
    });
  } catch {
    return [];
  }
}

// ==================== 主流程 ====================

async function main() {
  // 0. 确保数据目录
  const dataDir = process.env.WEFLOW_DATA_DIR || path.join(process.cwd(), '.weflow-data');
  ensureDataDir(dataDir);

  // 1. 交互式配置引导（如需要）
  const setupOk = await guidedSetup(CONFIG_PATH);
  if (!setupOk) {
    console.error('[weflow-core] 配置失败，退出');
    process.exit(1);
  }

  // 2. 重新加载完整配置（使用传入的配置路径）
  const { ConfigService } = require('./services/config');
  const { wcdbService } = require('./services/wcdbService');

  // 重置实例以重新加载
  ConfigService.resetInstance();
  const config = ConfigService.getInstance(CONFIG_PATH);
  config.init();

  const dbPath = config.get('dbPath');
  const myWxid = config.get('myWxid');
  const wxidConfigs = config.get('wxidConfigs') || {};
  const decryptKey = wxidConfigs[myWxid]?.decryptKey || config.get('decryptKey');

  if (!dbPath || !myWxid || !decryptKey) {
    console.error('[weflow-core] 配置不完整: dbPath/wxid/decryptKey 缺失');
    process.exit(1);
  }

  // 3. 设置资源路径（强制转为绝对路径）
  let resourcesPath = config.get('resourcesPath')
    || path.join(__dirname, '..', 'resources');
  if (!path.isAbsolute(resourcesPath)) {
    resourcesPath = path.resolve(process.cwd(), resourcesPath);
  }
  wcdbService.setPaths(resourcesPath, config.getUserDataPath());
  console.log(`[weflow-core] 资源路径: ${resourcesPath}`);

  // 4. 打开数据库
  const accountDir = config.getAccountDir(dbPath, myWxid);
  if (!accountDir) {
    console.error('[weflow-core] 未找到账号目录');
    process.exit(1);
  }
  console.log(`[weflow-core] 账号目录: ${accountDir}`);

  const openResult = await wcdbService.open(accountDir, decryptKey);
  if (!openResult) {
    console.error('[weflow-core] 数据库打开失败');
    const lastErr = await wcdbService.getLastInitError();
    if (lastErr) console.error(`  错误详情: ${lastErr}`);
    process.exit(1);
  }
  console.log('[weflow-core] ✅ 数据库已连接');

  // 4.5 设置独立缓存目录（避免与安装版 WeFlow 冲突）
  const cachePath = config.get('cachePath') || path.join(config.getUserDataPath(), 'image-cache')
  config.set('cachePath', cachePath)
  console.log(`[weflow-core] 缓存目录: ${cachePath}`)

  // 5. 初始化 chatService
  const { chatService } = require('./services/chatService');
  await chatService.connect();
  console.log('[weflow-core] ✅ chatService 已就绪');

  // 5.5 预热会话信息（加载群显示名、联系人备注，确保 SSE 推送用显示名而非原始 ID）
  const sessionsResult = await chatService.getSessions()
  if (sessionsResult.success && sessionsResult.sessions) {
    const usernames = sessionsResult.sessions.map((s: any) => s.username)
    await chatService.enrichSessionsContactInfo(usernames)
    console.log(`[weflow-core] ✅ 会话信息已预热 (${usernames.length} 个会话)`)
  }

  // 6. 启动消息推送（需先连接数据库变更监听）
  const { messagePushService } = require('./services/messagePushService');
  chatService.addDbMonitorListener((type: string, json: string) => {
    messagePushService.handleDbMonitorChange(type, json)
  })
  messagePushService.start();
  console.log('[weflow-core] ✅ 消息推送已启动');

  // 7. 启动 HTTP API
  const { httpService } = require('./services/httpService');
  const apiPort = config.get('apiPort') || 5031;
  const apiHost = config.get('apiHost') || '127.0.0.1';
  const httpResult = await httpService.start(apiPort, apiHost);
  if (!httpResult.success) {
    console.error(`[weflow-core] HTTP API 启动失败: ${httpResult.error}`);
    process.exit(1);
  }
  console.log(`[weflow-core] ✅ HTTP API 已启动: http://${apiHost}:${apiPort}`);

  // 8. 就绪
  console.log('[weflow-core] ============================');
  console.log('[weflow-core] 🟢 服务就绪');
  console.log('[weflow-core] ============================');

  // 9. 优雅退出处理
  let shuttingDown = false;

  async function shutdown() {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log('\n[weflow-core] 正在优雅退出...');
    try { messagePushService.stop(); } catch { }
    try { await httpService.stop(); } catch { }
    try { chatService.close(); } catch { }
    try { wcdbService.close(); } catch { }
    console.log('[weflow-core] 已退出');
    process.exit(0);
  }

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  process.on('SIGBREAK', shutdown);

  // 防止未处理异常导致僵尸进程
  process.on('uncaughtException', (err) => {
    console.error('[weflow-core] 未捕获异常:', err.message);
    shutdown();
  });

  process.on('unhandledRejection', (reason) => {
    console.error('[weflow-core] 未处理的 Promise 拒绝:', reason);
  });
}

main().catch((err) => {
  console.error('[weflow-core] 启动失败:', err);
  process.exit(1);
});
