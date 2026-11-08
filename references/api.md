# Buddy 加油站 · 接口契约速查

> 全部接口均为实机联调验证（WorkBuddy Desktop 5.3.8 / 2026-10-27），非推测。
> 这些是客户端自用接口，官方未公开，可能随版本变更。脚本已做主备 endpoint 切换与明确报错。

## 鉴权

**凭据来源**：本机客户端登录会话（明文 JSON，只读）

```
Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
```

结构：

```json
{
  "account": { "uid": "<uuid>", "nickname": "...", "enterpriseId": "" },
  "auth":    { "accessToken": "<jwt>", "refreshToken": "<jwt>",
               "expiresAt": 1795454681453, "domain": "www.codebuddy.cn" },
  "accounts": []
}
```

**请求头**：

| Header | 值 |
| --- | --- |
| `Authorization` | `Bearer {accessToken}` |
| `X-User-Id` | `{account.uid}` |
| `Content-Type` | `application/json` |
| `Accept` | `application/json` |
| `X-Enterprise-Id` / `X-Tenant-Id` | 企业账号必填（`enterpriseId` 非空时） |

**Endpoint**：

| 用途 | 地址 |
| --- | --- |
| 主 | `https://copilot.tencent.com` |
| 备 | `https://www.codebuddy.cn` |

网关是 APISIX；无 token 时返回 `401 Authorization Required`。
响应统一包裹：`{"code":0,"msg":"OK","requestId":"<uuid>","data":{...}}`，**`code != 0` 即为业务错误**。

---

## 一、每日签到

### 1.1 签到活动状态（只读）

```
POST /v2/billing/meter/checkin-activity-status
body: {}
```

实测返回 `data`：

```json
{
  "active": true,
  "today_checked_in": true,
  "streak_days": 27,
  "daily_credit": 100,
  "today_credit": 100,
  "is_streak_day": false,
  "next_streak_day": 0,
  "streak_bonus_days": 0,
  "streak_bonus_credit": 0,
  "checkin_dates": ["2026-10-27", "2026-10-26", "..."],
  "week_checkin_days": 2,
  "week_progress": [true,true,false,false,false,false,false],
  "total_credits": 2700,
  "start_time": "2026-09-30 00:00:00",
  "end_time": "2026-12-31 23:59:59",
  "theme_name": "Buddy加油站",
  "season": 10,
  "activity_name": "Buddy应用",
  "claim_button_text": "立即领取",
  "action_button": { "show": true, "text": "体验Buddy应用",
                     "action": "workbuddy://home?openBuddySwitcher=true" }
}
```

### 1.2 领取每日签到

```
POST /v2/billing/meter/daily-checkin
body: {}
```

业务拒绝（已签到 / 活动结束）通过 `code` + `msg` 区分，**不抛 HTTP 异常**。
调用后建议回读 1.1 确认，避免误报。

---

## 二、派喵喵旅行

### 2.1 地点配置（只读）

```
GET /v2/activity/growth/buddy/travel/config
```

实测 4 个地点：

| id | code | name | 时长(h) | 奖励 |
| --- | --- | --- | --- | --- |
| 1 | coffee | 咖啡馆 | 1~4 | 5~10 |
| 2 | mall | 商场店铺 | 1~4 | 5~10 |
| 3 | gym | 健身房 | 1~4 | 5~10 |
| 4 | ancient_town | 古镇客栈 | 1~4 | 5~10 |

同时返回 `intro_slogans[]`、`intro_slogan_items[]`、`server_now`。

### 2.2 旅行状态（只读）

```
GET /v2/activity/growth/buddy/travel/status
```

```json
{
  "state": "arrived",              // idle | traveling | arrived
  "buddy_id": 5697110,
  "record_id": 26242643,
  "location": {"id":1,"code":"coffee","name":"咖啡馆","duration_hours":4},
  "depart_at": 1792631395,
  "arrive_at": 1792645795,
  "server_now": 1793036023,
  "letter": {"id":2,"text":"亲爱的铲屎官：...","guide_text":"..."},
  "use_deeplink": "workbuddy://discover?cardId=...",
  "daily_limit_reached": false,
  "duration_hours": 4,
  "reward_credit": 9
}
```

### 2.3 派 Buddy 出发

```
POST /v2/activity/growth/buddy/travel/depart
body: {"location_id": 1}
```

- 200 → `state:"traveling"`，服务端**随机**分配 `duration_hours`（1~4）与 `arrive_at`
- 已在旅行中 → `403 already traveling`
- **参数名必须是 `location_id`**；传 `location_code` / `id` 会 `400 invalid request`

### 2.4 收明信片领积分

```
POST /v2/activity/growth/buddy/travel/claim
body: {}
```

200 → 返回 `reward_credit`（实测 9）与 `letter` 正文，状态回到 `idle`，可立即再出发下一轮。

### 状态机

```
idle ──depart──> traveling ──(等待 arrive_at)──> arrived ──claim──> idle
                     │                                                │
                     └── 403 already traveling                        └── daily_limit_reached 则停止
```

---

## 三、成长计划

### 3.1 Buddy 信息（只读）

```
GET /v2/activity/growth/buddy/info      ← 必须 GET，POST 会 404
```

```json
{"buddy": {"instance_id":5697110,"name":"暗影喵","personality":"安全守护者",
           "rarity":"SR","soul_desc":"...","appearance":{...}},
 "poll_interval_seconds": 300}
```

### 3.2 任务列表（只读）

```
GET /v2/activity/growth/tasks
```

`data.tasks[]`：

| 字段 | 说明 |
| --- | --- |
| `task_code` | 任务标识，如 `playbook_prompt` |
| `title` / `description` / `task_desc` | 文案 |
| `reward_credit` / `reward_energy` | 奖励积分 / 能量 |
| `accept_status` | `not_accepted` / `accepted` / `completed` / `claimed` |
| `progress` | `{current, target}`，未接受时为 `null` |

> ⚠️ **踩坑**：任务达标后服务端返回的是 **`completed`**，不是 `accepted`。
> 只判断 `accepted` 会把已完成未领的奖励全部漏掉（实测一次漏 300 分）。
> 稳妥做法：`claimed` → 跳过；`not_accepted` → 可接受；**其余一律按 `progress.current >= target` 判定可领**。
| `locked` | 是否锁定 |
| `jump_url` | 跳转链接（多为 `workbuddy://` 协议） |

实测任务奖励档位：50 / 100 / 300 分。

### 3.3 批量接受任务

```
POST /v2/activity/growth/tasks/accept
body: {"task_codes": ["playbook_prompt", "chat_5"]}
```

**参数名是复数 `task_codes`（数组）**，传单数 `task_code` 会 `400 invalid request`。
返回：`{"results":[{"task_code":"playbook_prompt","status":"accepted"}]}`

### 3.4 领取单个任务奖励

```
POST /v2/activity/growth/tasks/{task_code}/claim
body: {}
```

- 200 → `{"already_claimed": true/false, "credit": N, "energy": N}`
- 未完成 → `400 task not completed`

---

## 四、其它（可选）

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/v2/activity/workbuddy/banner` | GET | 活动横幅，当前 `code:12302 activity is offline` |
| `/v2/activity/ambassador/status` | GET | 推广大使状态 |

---

## 错误码对照

| code | 含义 | 处理 |
| --- | --- | --- |
| 0 | 成功 | — |
| 400 | 参数错误 / 任务未完成 | 检查参数名（`location_id` / `task_codes`） |
| 403 | 已在旅行中 | 回读 status 继续 |
| 12302 | 活动已下线 | 跳过 |
| 401（HTTP） | 凭据失效 | 提示重新登录客户端 |

## 已排除的无效路径（别再试）

```
/v2/activity/growth/buddy/info            + POST  → 404（必须 GET）
/v2/activity/growth/tasks/accept          + {task_code} → 400
/v2/activity/growth/buddy/travel/locations|/list|/letter|/arrive → 404
/v2/operation-platform/slots/active       → 404
/v2/billing/meter/get-user-resource       → 404
/profile/usage  /profile/plans-usage      → 返回 HTML 页面，不是 API
```
