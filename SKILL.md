---
name: buddy-credits
display_name: Buddy 积分助手
display_name_en: Buddy Credits Assistant
description: |
  Buddy 积分助手 —— 自动领取 WorkBuddy「Buddy 加油站」积分。读取本机登录态，直查加油站接口，
  完成每日签到、派喵喵旅行（出发/到达/收明信片领积分）、成长计划任务领奖，生成每日积分播报并推送到
  飞书/企业微信/钉钉/Telegram/Server酱等 IM 终端。支持幂等重复运行、试运行巡检、无人值守定时任务。
  当用户提到「领积分」「签到」「加油站」「派喵喵旅行」「Buddy 旅行」「成长计划」「积分播报」
  「每日积分」「自动签到」时使用本技能。
description_zh: >
  自动领取 WorkBuddy「Buddy 加油站」积分：每日签到、派喵喵旅行（出发/收明信片/领积分）、
  成长计划任务领奖，生成每日积分播报并推送到飞书、企业微信、钉钉、Telegram 等 IM。
  幂等可重复运行，支持试运行巡检与无人值守定时任务，客户端关着也能自动签到。
description_en: >
  Automatically claim WorkBuddy "Buddy Fuel Station" credits: daily check-in, Buddy cat travel
  (depart / collect postcard / claim), and growth-plan task rewards. Generates a daily credit
  digest and pushes it to Feishu, WeCom, DingTalk, Telegram, ServerChan or any custom webhook.
  Idempotent, dry-run capable, and designed for unattended scheduled runs.
category: productivity
version: 1.1.0
author: 马总
---

# Buddy 积分助手（buddy-credits）

把 WorkBuddy「Buddy 加油站」每天能领的积分一次性拿满，并把日报推到你的 IM。

## 它能做什么

| 模块 | 能力 | 是否改状态 |
| --- | --- | --- |
| 每日签到 | 读取签到状态 → 未签则领取，记录连签天数 | 是（幂等） |
| 派喵喵旅行 | 状态机驱动：idle→出发，arrived→收明信片领积分，traveling→等待 | 是（幂等） |
| 成长计划任务 | 只读任务进度 + 领取**已完成**任务的奖励 | 是（幂等） |
| Buddy 信息 | 只读成长计划里的 Buddy（名字/稀有度/性格） | 否 |
| 积分播报 | 生成文本 / Markdown / JSON 日报 | 否 |
| IM 推送 | 飞书、企业微信、钉钉、Telegram、Server酱、自定义 webhook | 否 |

**边界（重要）**：任务本身（「对话 1 次」「召唤 5 次专家」等）需要真实使用才能完成。
本技能**不伪造完成**，只负责把已完成的奖励领掉、并把剩余任务列进待办提醒。

## 快速开始

```bash
# 0) 先自检本机登录态与接口连通性
python scripts/buddy_credits.py doctor

# 1) 只读巡检，看看今天能领多少（不改任何状态）
python scripts/buddy_credits.py run --dry-run

# 2) 真正跑闭环
python scripts/buddy_credits.py run

# 3) 跑完推送到 IM
python scripts/buddy_credits.py run --notify
```

## 命令参考

```
doctor                          本机登录态 + 4 个接口连通性自检
run                             跑完整闭环（签到 + 旅行 + 任务）
  --dry-run                     只读，不做任何领取
  --notify                      推送日报到 IM
  --channel feishu wecom       只推送指定通道
  --wait                        阻塞等待旅行到达，拿满当天旅行次数
  --only checkin|travel|tasks|buddy
                                只跑指定模块
  --accept-tasks                自动接受未接受的任务（默认开启，接受后才开始算进度）
  --no-accept-tasks             不自动接受新任务
  --location max_reward|random|fixed:1
                                旅行地点策略，默认挑奖励上限最高的
  --json / --markdown           机器可读输出
  -v, --verbose                 打印每个请求的状态码与 requestId
notify                          只推送最近一次报告
```

退出码：`0` 成功 / `1` 部分模块失败 / `2` 凭据缺失或过期 / `3` 网络不可达。

## 凭据怎么来的

脚本**只读**本机 WorkBuddy 客户端的登录会话文件：

```
Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

- 不需要客户端正在运行，只要文件在且 token 未过期（客户端开着时会自动续期并回写）
- 脚本会按序探测多个候选路径；也可设 `BUDDY_CREDITS_AUTH_FILE` 强制指定
- **token 只在内存中，不落配置、不进日志、不进推送内容**，任何输出均为掩码

如果 `doctor` 报「未找到会话」：打开 WorkBuddy 桌面端登录一次即可。

### 桌面端 5.9.3+ 字段级加密（已自动处理）

从 5.9.3 起，`auth.accessToken` / `refreshToken` / `account.nickname` 会被写成
`{"$wbEncrypted":1,"envelope":"<base64>"}`（AES-256-GCM）。脚本检测到密文后会自动调用
`scripts/wb_token.js` 就地解密，对调用方完全透明，无需任何额外参数。

解密原理（用于排障）：

1. 保护密钥来自 Electron 二进制的内部绑定
   `electron_browser_workbuddy_storage.loggerGet()`，返回的 JSON 里 `atRestSecretKey`
   经 `sha256` 得到 32 字节静态保护密钥（`keyId` = 其 sha256 前 16 位十六进制，与
   `~/.workbuddy/keyblob` 的 `slots[0].protectorKeyId` 对得上）。
2. 字段密文用 `resources/app.asar/main/dist.js` 里的 `AtRestCrypto` 以
   `AAD(framing="field")` 解开；该模块是纯 Node 实现，可直接 `require`。
3. 因为要拿内部绑定，解密进程必须由 WorkBuddy 自带的 Electron 二进制以
   `ELECTRON_RUN_AS_NODE=1` 方式运行——普通 python 或系统 node 都拿不到密钥。

因此：

- **重新登录不能解决**加密本身（写回仍是密文），但**解密通道会自动生效**
- 找不到客户端时，可用 `BUDDY_CREDITS_DESKTOP_EXE` 指定 `WorkBuddy.exe` 路径
- 解密失败时 `run` 会以退出码 `2` 结束，并打印 `wb_token.js` 返回的失败阶段
  （`binding` / `loggerGet` / `codec` / `decrypt` 等），据此定位是客户端升级、
  安装路径变动还是 keyblob 被重建

## 配置 IM 推送

配置文件位置：`%APPDATA%\buddy-credits\config.json`（Windows）
macOS：`~/Library/Application Support/buddy-credits/config.json`
Linux：`~/.local/share/buddy-credits/config.json`

也可以设 `BUDDY_CREDITS_CONFIG` 指向自定义路径。

最小示例（同时推飞书 + 企业微信）：

```json
{
  "notify": {
    "feishu": {
      "enabled": true,
      "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx",
      "secret": ""
    },
    "wecom": {
      "enabled": true,
      "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxx"
    }
  }
}
```

完整字段与多通道配置见 `references/notify.md`。
密钥建议用 `${ENV_VAR}` 占位，避免明文入库。

## 无人值守

脚本是幂等的，重复运行不会重复领取。配一个定时任务即可「客户端关着也自动签」：

- **WorkBuddy 定时任务**：用 `automation_update` 建一个每日任务，prompt 写「运行 buddy-credits 技能，跑 run --notify」
- **Windows 计划任务**：`schtasks /create /sc daily /st 09:00 /tn buddy-credits /tr "python D:\path\to\buddy_credits.py run --notify"`
- **cron**：`0 9 * * * python3 /path/to/buddy_credits.py run --notify`

想一次拿满当天旅行次数，用 `run --wait --notify`（会阻塞到 Buddy 回来）。

## 幂等与幂等保证

- 签到：先读 `today_checked_in`，已签直接跳过；领取失败会回读状态确认，不会误报
- 旅行：严格按服务端 `state` 状态机推进；`403 already traveling` 会回读状态继续
- 任务：对**所有非 claimed 状态**的任务按「是否达标」判定后调 claim。
  ⚠️ 服务端已完成任务返回的 `accept_status` 是 **`completed`**（不是 `accepted`），
  只认 `accepted` 会把已完成的奖励整批漏领；现在统一走 `else` 分支按 `progress` 兜底。
  已领返回 `already_claimed`。
- 尊重服务端 `daily_limit_reached`，到顶即停，不硬刷

## 安全约定

1. token 只存在内存，日志/报告/推送中一律掩码
2. 只读客户端会话文件，不写、不改、不删客户端任何文件
3. 不伪造任务完成、不绕过频率限制；请求间隔 ≥ 500ms，失败指数退避
4. 推送内容仅含报告正文，不含任何凭据
5. 配置文件支持 `${ENV_VAR}` 占位

## 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `未找到本机登录会话` | 会话文件不存在 | 打开 WorkBuddy 桌面端登录一次；或设 `BUDDY_CREDITS_AUTH_FILE` |
| 四个接口全报 `401 Authorization Required`（返回 HTML，openresty/apisix） | auth 目录下有两份会话，脚本选中了 `workbuddy-desktop-ai.info`（`domain=www.workbuddy.ai`），该 token 调 `/v2/*` 一律 401 | 已修：`load_session` 会解析**所有** `.info` 后按 domain 排序，优先 `copilot.tencent.com` / `www.codebuddy.cn` 那一份。若仍复现，设 `BUDDY_CREDITS_AUTH_FILE=%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` 强制指定 |
| `登录凭据已过期` | token 过期 | 打开桌面端重新登录，客户端会自动回写会话文件 |
| 退出码 2 且提示「accessToken 已被桌面端加密」 | 5.9.3+ 加密，且自动解密通道没跑通 | 看随后打印的失败阶段：`binding`→客户端大版本不匹配；`loggerGet`→客户端未安装/被拦截；`decrypt`→keyblob 被重建，需在客户端重新登录一次让字段重新写入。可设 `BUDDY_CREDITS_DESKTOP_EXE` 指定 exe |
| 退出码 3 | 网络不可达 | 检查网络/代理；脚本会自动在主备 endpoint 间切换 |
| 旅行一直 traveling | 服务端分配的时长未到 | 用 `--wait` 阻塞等待，或下次运行再领 |
| 推送没收到 | 配置未生效 | 检查 `config.json` 路径；`doctor` 会打印已配置的通道 |

## 文件

```
scripts/buddy_credits.py     主程序（零第三方依赖，Python 3.8+）
scripts/wb_token.js          桌面端加密会话的解密器（需 WorkBuddy 自带 Electron 以 RUN_AS_NODE 执行）
references/api.md            接口契约速查（含实测返回）
references/notify.md         IM 推送配置指南
assets/config.example.json   配置模板
```

接口全表见 `references/api.md`，全部为实机联调验证过的真实契约。

---

## 版本历史

### v1.1.0+local（本机补丁，2026-11-02）
- **修复选错会话导致全接口 401**：本机 auth 目录下同时存在
  `workbuddy-desktop.info`（`domain=copilot.tencent.com`，可用）与
  `workbuddy-desktop-ai.info`（`domain=www.workbuddy.ai`，调 `/v2/*` 被 APISIX 打回 401）。
  旧逻辑「取第一个能解析的」按文件名排序恰好选中 -ai 那份，doctor 4/4 全红。
  现改为收集全部候选后按 domain 命中官方域名排序，同名优先 `workbuddy-desktop.info`。
- dry-run 日报文案修正：`已接受新任务 N 个` → `待接受新任务 N 个（试运行未执行）`，避免误以为 dry-run 改了状态。

### v1.1.0（2026-10-28）
- **修复严重漏领 bug**：服务端对「已达标任务」返回 `accept_status="completed"`（而非 `accepted`），
  旧版只认 `accepted` 导致已完成的奖励整批漏领（实测一次漏 300 分）。现改为
  `claimed` 跳过 / `not_accepted` 可接受 / **其余一律按 `progress.current >= target` 判定可领**。
- `--accept-tasks` 改为**默认开启**（新增 `--no-accept-tasks` 关闭）：只有接受任务后服务端才开始计算进度。
- 日报新增「进行中 N 个（需在客户端实际使用才能达成）」与「待办提醒」清单，方便用户照着做、次日自动到账。
- 明确边界：任务本身（行为类）需真实使用才能完成，脚本**只领不伪造**。

### v1.0.0
- 初版：每日签到、派喵喵旅行、成长计划任务领奖、IM 推送、试运行巡检、幂等无人值守。
