#!/usr/bin/env node
/*
 * wb_token.js - 从 WorkBuddy 桌面端会话文件中取出明文 accessToken
 *
 * 背景: 桌面端 5.9.3 起对 auth.accessToken / refreshToken 做字段级静态加密
 *      ({"$wbEncrypted":1,"envelope":"<base64>"})。解密所需的保护密钥由
 *      Electron 二进制的内部绑定 electron_browser_workbuddy_storage.loggerGet()
 *      提供, 只在同用户进程中可用, 因此必须以
 *          ELECTRON_RUN_AS_NODE=1 <WorkBuddy.exe> wb_token.js
 *      方式运行, 并复用桌面端自带的加密模块 resources/app.asar/main/dist.js。
 *
 * 用法:
 *   wb_token.js --exe <WorkBuddy.exe> --info <workbuddy-desktop.info> [--out <token-file>]
 * 输出: 一行 JSON, 不含令牌本体; 令牌通过 --out 指定的文件传出(权限 0600)。
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith('--')) out[a.slice(2)] = argv[i + 1];
  }
  return out;
}

function fail(stage, message) {
  process.stdout.write(JSON.stringify({ ok: false, stage, error: String(message || '') }) + '\n');
  process.exit(0);
}

const args = parseArgs(process.argv.slice(2));
const exePath = args.exe;
const infoPath = args.info;
if (!exePath || !infoPath) fail('args', 'missing --exe or --info');

const asarDist = path.join(path.dirname(exePath), 'resources', 'app.asar', 'main', 'dist.js');
const keyblobPath = path.join(os.homedir(), '.workbuddy', 'keyblob');

let dist;
try {
  dist = require(asarDist);
} catch (e) {
  fail('require-dist', 'cannot require ' + asarDist + ': ' + e.message);
}
// 懒加载子模块: 调用 y() 后 dist.v 才是 AtRestCrypto 类, dist.E 才可用
try { dist.y(); } catch (e) { /* 某些版本无此导出 */ }

let binding;
try {
  binding = process._linkedBinding('electron_browser_workbuddy_storage');
} catch (e) {
  fail('binding', 'electron_browser_workbuddy_storage unavailable: ' + e.message);
}

let payloadStr;
try {
  payloadStr = binding.loggerGet();
} catch (e) {
  fail('loggerGet', e.message);
}

let payload;
try {
  payload = JSON.parse(payloadStr);
} catch (e) {
  fail('payload', 'loggerGet payload is not JSON');
}
if (!payload || typeof payload.atRestSecretKey !== 'string') fail('payload', 'atRestSecretKey missing');

// 保护密钥 = sha256(atRestSecretKey 的 base64 文本)
const protectorKey = {
  key: crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest(),
  keyId: crypto.createHash('sha256')
    .update(crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest())
    .digest('hex')
    .slice(0, 16),
};

const AtRest = dist.v;
if (typeof AtRest !== 'function') fail('atrest', 'AtRestCrypto class not resolved');

let codec;
try {
  codec = new AtRest(protectorKey);
} catch (e) {
  fail('codec', e.message);
}

function openField(wrapper) {
  if (typeof wrapper === 'string') return wrapper;
  if (!wrapper || typeof wrapper !== 'object' || wrapper.$wbEncrypted !== 1) return null;
  if (wrapper.scheme === 'asym-v1') throw new Error('asym-v1 envelope unsupported');
  const env = dist.E(Buffer.from(wrapper.envelope, 'base64'));
  return codec.open(env, { framing: 'field' }).toString('utf8');
}

let info;
try {
  info = JSON.parse(fs.readFileSync(infoPath, 'utf8'));
} catch (e) {
  fail('read-info', e.message);
}
const auth = info.auth || {};
const result = {
  ok: false,
  uid: (info.account || {}).uid || null,
  nickname: (info.account || {}).nickname || null,
  domain: auth.domain || '',
  expiresAt: auth.expiresAt || 0,
  refreshExpiresAt: auth.refreshExpiresAt || 0,
  enterpriseId: (info.account || {}).enterpriseId || '',
};

try {
  result.accessToken = openField(auth.accessToken) || '';
  result.refreshToken = openField(auth.refreshToken) || '';
  const nick = openField((info.account || {}).nickname);
  if (nick) result.nickname = nick;
} catch (e) {
  fail('decrypt', e.message);
}
if (!result.accessToken) fail('decrypt', 'accessToken could not be decrypted');

if (args.out) {
  fs.writeFileSync(args.out, result.accessToken, { mode: 0o600 });
}
delete result.accessToken;
delete result.refreshToken;
result.ok = true;
result.tokenFile = args.out || null;
result.tokenLength = (args.out && fs.existsSync(args.out)) ? fs.statSync(args.out).size : 0;
process.stdout.write(JSON.stringify(result) + '\n');
