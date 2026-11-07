// Execute the actual TypeScript methods with isolated I/O dependencies.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'weflow-core/node_modules/typescript'));
function harness(file, method, dependencies) {
  const source = ts.createSourceFile(file, fs.readFileSync(path.join(root, file), 'utf8'), ts.ScriptTarget.Latest, true);
  let member;
  function visit(node) {
    if (ts.isMethodDeclaration(node) && node.name.getText(source) === method) member = node;
    ts.forEachChild(node, visit);
  }
  visit(source); assert.ok(member, method);
  const js = ts.transpileModule(`class Harness { ${member.getText(source)} }; globalThis.Harness = Harness`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const context = vm.createContext({ ...dependencies, console });
  vm.runInContext(js, context);
  return new context.Harness();
}
async function main() {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'symmetry-weflow-'));
  try {
    const cached = path.join(folder, 'cached.gif');
    const data = Buffer.from('GIF89a-fixture-exact-original-bytes'); fs.writeFileSync(cached, data);
    const chat = harness('weflow-core/src/services/chatService.ts', 'downloadEmojiFile', { existsSync: fs.existsSync });
    let calls = 0;
    chat.downloadEmoji = async () => { calls++; return { success: true, localPath: cached }; };
    chat.fallbackEmoticon = async msg => { msg.emojiLocalPath = cached; };
    assert.equal(await chat.downloadEmojiFile({ emojiLocalPath: cached }), cached);
    assert.equal(await chat.downloadEmojiFile({ emojiMd5: 'fixture' }), cached);
    assert.equal(calls, 0);
    assert.equal(await chat.downloadEmojiFile({ emojiMd5: 'fixture', emojiCdnUrl: 'https://example.invalid/sticker' }), cached);
    assert.equal(calls, 1);
    assert.equal(await chat.downloadEmojiFile({}), null);
    const http = harness('weflow-core/src/services/httpService.ts', 'exportMediaForMessage', { fs, path, chatService: chat });
    http.sanitizeFileName = (value, fallback) => String(value || fallback).replace(/[^a-zA-Z0-9_.@-]/g, '_');
    http.ensureDir = dir => fs.mkdirSync(dir, { recursive: true });
    const result = await http.exportMediaForMessage({ localType: 47, localId: 3, emojiMd5: 'fixture', emojiLocalPath: cached },
      'test@chatroom', path.join(folder, 'exported'), { exportEmojis: true });
    assert.equal(result.kind, 'emoji'); assert.equal(path.extname(result.fullPath), '.gif');
    assert.deepEqual(fs.readFileSync(result.fullPath), data);
    assert.equal(await http.exportMediaForMessage({ localType: 47 }, 'test', folder, { exportEmojis: false }), null);
    assert.equal(await http.exportMediaForMessage({ localType: 47 }, 'test', folder, { exportEmojis: true }), null);
    console.log('PASS: cached sticker, MD5 fallback cache, CDN resolver, missing payload, exact GIF export and emoji-disabled gate');
  } finally { fs.rmSync(folder, { recursive: true, force: true }); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
