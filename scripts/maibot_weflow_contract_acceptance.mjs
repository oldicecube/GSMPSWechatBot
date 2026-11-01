// Exercise the real TypeScript row mapper, SSE builder and original-message API projection.
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import * as esbuild from '../weflow-core/node_modules/esbuild/lib/main.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const core = path.join(root, 'weflow-core');
const instance = path.join(root, 'temp', 'maibot-weflow-contract');
fs.mkdirSync(instance, { recursive: true });
const config = path.join(instance, 'config.json');
fs.writeFileSync(config, JSON.stringify({ weflow: { myWxid: 'wxid_contract_bot', cachePath: instance } }));
const outfile = path.join(core, 'dist', 'bridge-contract-test.cjs');
await esbuild.build({
  absWorkingDir: core,
  stdin: { resolveDir: core, sourcefile: 'bridge-contract-test.ts', loader: 'ts', contents: `
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {chatService} from './src/services/chatService';
import {messagePushService} from './src/services/messagePushService';
import {httpService} from './src/services/httpService';
(async()=>{
  const packed=57*4294967296+49;
  const xml='<msg><appmsg><title>引用回答</title><type>57</type><refermsg><svrid>quoted-id</svrid><content>引用正文</content></refermsg></appmsg></msg>';
  const row={local_id:123,server_id:'987654321098765432',local_type:packed,create_time:1793188305,is_send:0,sender_username:'wxid_sender',message_content:xml,source:'<msgsource><atuserlist><![CDATA[wxid_other,wxid_contract_bot]]></atuserlist></msgsource>'};
  const message=chatService.mapRowsToMessagesLiteForApi([row])[0];
  assert.equal(message.serverIdRaw,row.server_id);
  assert.deepEqual(chatService.getMessageAtUserIds(message),['wxid_other','wxid_contract_bot']);
  const push=messagePushService as any;
  push.configService.getMyWxidCleaned=()=> 'wxid_contract_bot';
  push.resolveGroupSourceName=async()=> '测试用户';
  push.normalizePushAvatarUrl=async()=> undefined;
  chatService.getContactAvatar=async()=>({displayName:'测试群'} as any);
  const payload=await push.buildPayload({username:'contract@chatroom',displayName:'测试群'},message);
  assert.equal(payload.localType,49);assert.equal(payload.localSubtype,57);assert.equal(payload.rawLocalType,packed);
  assert.equal(payload.serverId,row.server_id);assert.equal(payload.rawContent,xml);assert.equal(payload.senderWxid,'wxid_sender');assert.equal(payload.isSend,0);assert.equal(payload.isAt,true);
  assert.deepEqual(payload.atUsers,['wxid_other','wxid_contract_bot']);
  const api=(httpService as any).toApiMessage(message);
  assert.equal(api.serverId,row.server_id);assert.equal(api.senderWxid,'wxid_sender');assert.equal(api.baseLocalType,49);assert.equal(api.localSubtype,57);assert.deepEqual(api.atUsers,payload.atUsers);
  const report={passed:true,checks:['row_source_decode','exact_server_id','sse_packed_type','sse_sender_and_echo_fields','sse_raw_rich_content','sse_at_users_and_bot_trigger','original_api_projection']};
  fs.writeFileSync(process.env.CONTRACT_REPORT!,JSON.stringify(report,null,2));console.log(JSON.stringify(report));process.exit(0);
})().catch(error=>{console.error(error);process.exit(1)});
` },
  bundle: true, platform: 'node', target: 'node18', format: 'cjs', outfile,
  external: ['koffi', 'fzstd', 'jieba-wasm', 'silk-wasm', 'sherpa-onnx-node', 'ffmpeg-static', 'worker_threads'],
  plugins: [{ name: 'electron-stub', setup(build) {
    build.onResolve({ filter: /^electron$/ }, () => ({ path: path.join(core, 'src', 'electron-stub.ts') }));
    build.onResolve({ filter: /^electron-store$/ }, () => ({ path: 'store', namespace: 'stub' }));
    build.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'module.exports = {}', loader: 'js' }));
  } }],
});
const result = spawnSync(process.execPath, [outfile], { cwd: core, stdio: 'inherit', env: {
  ...process.env, WEFLOW_CONFIG_PATH: config, WEFLOW_DATA_DIR: instance,
  CONTRACT_REPORT: path.join(instance, 'acceptance.json'),
} });
process.exit(result.status ?? 1);
