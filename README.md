# Buddy 加油站积分助手

WorkBuddy「Buddy 加油站」每日积分自动领取：**每日签到 → 派喵喵旅行 / 收明信片 → 成长计划领奖**，跑完输出一份日报，可选推送到飞书 / 企业微信 / Telegram / Server 酱。

零第三方依赖，Python 3.8+ 直接跑。

```
Buddy 加油站日报 · 2026-11-08
──────────────────────────────────────────
搭档:暗影喵 · SR 安全守护者
✅ 每日签到   连签 10 天,今日已签
            累计签到积分 1000（第 10 期 · Buddy加油站）
🐱 派喵喵旅行 收明信片 +9（咖啡馆）
📋 成长计划   无待领奖励,13 个任务进行中
──────────────────────────────────────────
💰 今日合计  +109 积分
```

## 用法

```bash
python scripts/buddy_credits.py doctor          # 自检:会话文件 + 4 个接口连通性
python scripts/buddy_credits.py run             # 完整闭环(签到 + 旅行 + 领任务奖)
python scripts/buddy_credits.py run --dry-run   # 只巡检,不做任何领取
python scripts/buddy_credits.py run --only travel
python scripts/buddy_credits.py run --notify    # 跑完并推送到已配置的 IM
python scripts/buddy_credits.py run --wait      # 阻塞等待旅行到达,拿满当天次数
```

退出码：`0` 成功 / `1` 部分模块失败 / `2` 凭据缺失或失效 / `3` 网络不可达。

脚本是**幂等**的：重复运行不会重复领取，签到/旅行已完成后会直接跳过。

## 凭据怎么来的

脚本**只读**本机客户端的登录会话文件，token 只留在内存，不进日志、不进推送：

```
Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

### 桌面端 5.9.3+ 的字段级加密

从 5.9.3 起，`auth.accessToken` / `refreshToken` / `account.nickname` 会被写成
`{"$wbEncrypted":1,"envelope":"<base64>"}`（AES-256-GCM），直接读文件拿到的是密文。
本仓库的 `scripts/wb_token.js` 会自动把它们解开，对调用方透明。

原理（也是自己排障时需要的全部信息）：

1. 保护密钥来自 Electron 二进制的内部绑定
   `electron_browser_workbuddy_storage.loggerGet()`，其返回 JSON 里的 `atRestSecretKey`
   做一次 `sha256` 即得 32 字节保护密钥（`keyId` = 其 sha256 前 16 位十六进制，
   与 `~/.workbuddy/keyblob` 中 `slots[0].protectorKeyId` 对应）。
2. 字段密文用客户端自带的 `resources/app.asar/main/dist.js` 中 `AtRestCrypto` 解开，
   `AAD` 为 `framing=field`。该模块是纯 Node 实现，可直接 `require`。
3. 因为要用内部绑定，解密进程必须由客户端自带的 Electron 二进制以
   `ELECTRON_RUN_AS_NODE=1` 运行——系统 python / node 拿不到那把密钥。

所以：**重新登录不能解除加密本身**（写回仍是密文），真正需要的是走上面这条解密通道。

两个实现上的坑，改代码时容易踩：

- `dist.js` 是懒加载的，不先调 `dist.y()` 初始化，`AtRestCrypto`（`dist.v`）就是 `undefined`，
  会报 `Cannot read properties of undefined (reading 'has')`。
- 会话字段用的是**保护密钥**直接加密（envelope 的 `keyId` 等于 `protectorKeyId`），
  而不是 keyblob 里的 master key；用错会报 `ciphertext declares a different key id`。

也可以用环境变量指定路径：

```bash
BUDDY_CREDITS_AUTH_FILE=...        # 强制指定会话文件
BUDDY_CREDITS_DESKTOP_EXE=...      # 强制指定 WorkBuddy.exe
BUDDY_CREDITS_CONFIG=...           # 自定义配置文件
```

## 推送配置

配置文件：`%APPDATA%\buddy-credits\config.json`（Windows），
macOS `~/Library/Application Support/buddy-credits/config.json`，
Linux `~/.local/share/buddy-credits/config.json`。模板见 `assets/config.example.json`。

## 目录

```
scripts/buddy_credits.py     主程序(零第三方依赖)
scripts/wb_token.js          加密会话的解密器(需客户端自带 Electron 以 RUN_AS_NODE 执行)
references/api.md            接口契约速查(含实测返回)
references/notify.md         IM 推送配置指南
assets/config.example.json   配置模板
SKILL.md                     技能描述(WorkBuddy Skill 格式)
```

## 安全约定

- token 只存在内存，输出一律掩码
- 只读客户端会话文件，不写、不改、不删客户端任何文件
- 不伪造任务完成、不绕过频率限制；请求间隔 ≥ 500ms，失败指数退避
- 推送内容仅含报告正文，不含任何凭据

## 免责

仅供个人学习与自用。接口与加密方案均由客户端版本决定，客户端升级后可能失效；
请自行评估是否符合你所适用的产品条款，因使用产生的后果由使用者承担。
