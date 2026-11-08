# IM 推送配置指南

配置文件位置：

| 系统 | 路径 |
| --- | --- |
| Windows | `%APPDATA%\buddy-credits\config.json` |
| macOS | `~/Library/Application Support/buddy-credits/config.json` |
| Linux | `~/.local/share/buddy-credits/config.json` |

可用 `BUDDY_CREDITS_CONFIG` 环境变量覆盖路径。
可用 `BUDDY_CREDITS_STATE_DIR` 覆盖整个状态目录（配置 + 日志 + 账本）。

模板文件：`assets/config.example.json`

---

## 密钥安全

任何字段都支持 `${ENV_VAR}` 占位，运行时从环境变量展开：

```json
{ "notify": { "feishu": { "webhook_url": "${BUDDY_FEISHU_WEBHOOK}" } } }
```

建议把 webhook / token 放进环境变量，配置文件只留占位符。

---

## 通用字段

每个通道块都支持：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 设为 `false` 可临时停用 |
| 通道专属字段 | — | 见下 |

同一通道可以配**多个**（数组形式），脚本会依次推送。

---

## 飞书

```json
"feishu": {
  "enabled": true,
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
  "secret": ""
}
```

`secret` 非空时自动做 HMAC-SHA256 签名（`timestamp` + `sign`），符合飞书安全设置要求。

## 企业微信

```json
"wecom": {
  "enabled": true,
  "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx"
}
```

## 钉钉

```json
"dingtalk": {
  "enabled": true,
  "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxxxxxxx",
  "secret": "SECxxxxxxxx"
}
```

`secret` 非空时自动加签（`timestamp` + `sign`）。

## Telegram

```json
"telegram": {
  "enabled": true,
  "bot_token": "123456:ABC-DEF",
  "chat_id": "-100xxxxxxxx"
}
```

## Server 酱

```json
"serverchan": {
  "enabled": true,
  "send_key": "SCTxxxxxxxx"
}
```

## 自定义 Webhook

```json
"custom": [
  {
    "enabled": true,
    "name": "我的内网机器人",
    "url": "https://internal.example.com/hook",
    "method": "POST",
    "headers": { "Authorization": "Bearer ${MY_TOKEN}" },
    "content_type": "application/json",
    "body_template": "{\"msgtype\":\"text\",\"text\":{\"content\":\"{title}\\n{text}\"}}"
  }
]
```

`{title}` / `{text}` 会被替换为日报标题与正文（换行已转义）。
若 `body_template` 不是合法 JSON，脚本会按 `content_type` 原样发送字符串。

---

## 多通道示例（一次推 3 个终端）

```json
{
  "notify": {
    "feishu": [
      { "enabled": true, "webhook_url": "${FEISHU_OPS}" },
      { "enabled": true, "webhook_url": "${FEISHU_SELF}", "secret": "${FEISHU_SECRET}" }
    ],
    "wecom": { "enabled": true, "webhook_url": "${WECOM_KEY}" },
    "telegram": { "enabled": true, "bot_token": "${TG_TOKEN}", "chat_id": "${TG_CHAT}" },
    "custom": [
      { "enabled": true, "url": "https://example.com/hook",
        "body_template": "{\"text\":\"{title}\\n{text}\"}" }
    ]
  }
}
```

---

## 验证

```bash
# 检查已配置的通道
python scripts/buddy_credits.py doctor

# 只推送最近一次报告（不重新跑领取）
python scripts/buddy_credits.py notify

# 只推指定通道
python scripts/buddy_credits.py notify --channel feishu
```

推送失败不会中断主流程，结果会写进日志与 `--json` 的 `push` 字段。

---

## 推送内容示例

```
Buddy 加油站日报 · 2026-10-27 · +109 积分

Buddy 加油站日报 · 2026-10-27
──────────────────────────────────────────
搭档:暗影喵 · SR 安全守护者
✅ 每日签到   连签 27 天,本次 +100
            累计签到积分 2700（第 10 期 · Buddy加油站）
🐱 派喵喵旅行 收明信片 +9（咖啡馆）
   📮 来自咖啡馆:亲爱的铲屎官：路过一条街...
📋 成长计划   领取奖励 +100
──────────────────────────────────────────
💰 今日合计  +109 积分
🎯 待办提醒
   · 探索优秀灵感(0/1) +100
   · 体验「资料库」(0/1) +100
```

推送正文**不含任何凭据、token、uid**。
