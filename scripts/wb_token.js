#!/usr/bin/env node
/*
 * wb_token.js - 从 WorkBuddy 桌面端会话文件中取出明文 accessToken
 *
 * 背景: 桌面端 5.9.3 起对 auth.accessToken / refreshToken 做字段级静态加密
 *      ({"$wbEncrypted":1,"envelope":"<base64>"})。解密所需的保护密钥由
 *      Electron 二进制的内部绑定 electron_browser_workbuddy_storage.loggerGet()
 *      提供, 只在同用户进程中可用, 因此必须以
 *          ELECTRON_RUN_AS_NODE=1 <WorkBuddy.exe> wb_token.js
 *      方式运行, 并复用客户端自带的加密模块 app.asar 里的 AtRestCrypto。
 *
 * 版本适配: 该模块的位置与导出名随版本变化(因此不写死) ——
 *   5.9.x  main/dist.js  AtRestCrypto = exports.v  parseSupportedEnvelope = exports.E
 *   5.11.x main/src.js   AtRestCrypto = exports.p  parseSupportedEnvelope = exports.x
 *   这里按"符号特征"解析: 找到原型上带 open/seal 的类; 导出为懒加载,
 *   必要时逐个调用零参导出触发初始化, 直到类出现。
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

const MODULE_CANDIDATES = ['main/src.js', 'main/dist.js', 'main/index.js'];

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i].startsWith('--')) out[argv[i].slice(2)] = argv[i + 1];
  }
  return out;
}

function fail(stage, message) {
  process.stdout.write(JSON.stringify({ ok: false, stage, error: String(message || '') }) + '\n');
  process.exit(0);
}

function isCodecClass(v) {
  return typeof v === 'function' && v.prototype
    && typeof v.prototype.open === 'function'
    && typeof v.prototype.seal === 'function';
}

function exportKeys(mod) {
  try { return Object.keys(mod); } catch (e) { return []; }
}

// 触碰所有 getter, 再按原型特征找 codec 类
function findCodec(mod) {
  for (const k of exportKeys(mod)) {
    let v;
    try { v = mod[k]; } catch (e) { continue; }
    if (isCodecClass(v)) return v;
  }
  return null;
}

function findParseEnvelope(mod) {
  for (const k of exportKeys(mod)) {
    let v;
    try { v = mod[k]; } catch (e) { continue; }
    if (typeof v === 'function' && v.name === 'parseSupportedEnvelope') return v;
  }
  return null;
}

// 导出是懒加载的: 逐个调用零参导出, 直到 codec 类被初始化出来
function forceInit(mod) {
  for (const k of exportKeys(mod)) {
    let v;
    try { v = mod[k]; } catch (e) { continue; }
    if (typeof v !== 'function' || v.length !== 0) continue;
    try { v(); } catch (e) { /* 部分零参导出是查询/清理, 调用失败无妨 */ }
    const cls = findCodec(mod);
    if (cls) return { codec: cls, via: k };
  }
  return { codec: null, via: null };
}

function loadCryptoModule(asarRoot) {
  const notes = [];
  for (const rel of MODULE_CANDIDATES) {
    const p = path.join(asarRoot, rel);
    if (!fs.existsSync(p)) { notes.push(rel + ':missing'); continue; }
    let mod;
    try {
      mod = require(p);
    } catch (e) {
      notes.push(rel + ':require-fail(' + (e.message || '').split('\n')[0].slice(0, 60) + ')');
      continue;
    }
    let Codec = findCodec(mod);
    let via = Codec ? 'direct' : null;
    if (!Codec) {
      const r = forceInit(mod);
      Codec = r.codec;
      via = r.via;
    }
    const parseEnv = findParseEnvelope(mod);
    if (Codec && parseEnv) return { Codec, parseEnv, modulePath: p, via, notes };
    notes.push(rel + ':codec=' + !!Codec + ',parse=' + !!parseEnv);
  }
  return { error: notes.join('; ') || 'no candidate module' };
}

const args = parseArgs(process.argv.slice(2));
const exePath = args.exe;
const infoPath = args.info;
if (!exePath || !infoPath) fail('args', 'missing --exe or --info');

const asarRoot = path.join(path.dirname(exePath), 'resources', 'app.asar');
const loaded = loadCryptoModule(asarRoot);
if (loaded.error) fail('module', loaded.error);
const Codec = loaded.Codec;
const parseEnv = loaded.parseEnv;

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
const protectorKey = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
const keyId = crypto.createHash('sha256').update(protectorKey).digest('hex').slice(0, 16);

let codec;
try {
  codec = new Codec({ key: protectorKey, keyId });
} catch (e) {
  fail('codec', e.message);
}

function openField(wrapper) {
  if (typeof wrapper === 'string') return wrapper;
  if (!wrapper || typeof wrapper !== 'object' || wrapper.$wbEncrypted !== 1) return null;
  if (wrapper.scheme === 'asym-v1') throw new Error('asym-v1 envelope unsupported');
  const env = parseEnv(Buffer.from(wrapper.envelope, 'base64'));
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
  cryptoModule: loaded.modulePath,
  initVia: loaded.via,
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
  result.tokenFile = args.out;
  result.tokenLength = fs.statSync(args.out).size;
}
delete result.accessToken;
delete result.refreshToken;
result.ok = true;
process.stdout.write(JSON.stringify(result) + '\n');
