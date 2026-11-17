#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Buddy 积分助手 (buddy-credits)
===============================
只读本机 WorkBuddy 登录态 -> 直查 Buddy 加油站接口 -> 幂等领取 -> 生成积分播报 -> 推送到 IM。

零第三方依赖，Python 3.8+ 可直接运行。

用法:
    python buddy_credits.py doctor                 # 检查本机登录态是否正常
    python buddy_credits.py run                    # 跑完整闭环(签到+旅行+领任务奖)
    python buddy_credits.py run --notify           # 跑完并推送到配置的所有 IM
    python buddy_credits.py run --wait             # 阻塞等待旅行到达,拿满当天旅行次数
    python buddy_credits.py run --dry-run          # 只读巡检,不做任何领取
    python buddy_credits.py run --only travel      # 只跑旅行模块
    python buddy_credits.py run --accept-tasks     # 自动接受未接受的任务
    python buddy_credits.py run --json             # 输出机器可读 JSON
    python buddy_credits.py notify                 # 只推送最近一次报告

退出码:
    0 成功   1 部分模块失败   2 凭据缺失/失效   3 网络不可达
"""

import argparse
import hashlib
import hmac
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import base64
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VERSION = "1.1.1"
SKILL_NAME = "buddy-credits"

# 主备 endpoint(来自 cli/product.json 的 endpoint / 备用域名)
ENDPOINTS = [
    "https://copilot.tencent.com",
    "https://www.codebuddy.cn",
]

# 接口路径
API_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"
API_CHECKIN_CLAIM = "/v2/billing/meter/daily-checkin"
API_TRAVEL_CONFIG = "/v2/activity/growth/buddy/travel/config"
API_TRAVEL_STATUS = "/v2/activity/growth/buddy/travel/status"
API_TRAVEL_DEPART = "/v2/activity/growth/buddy/travel/depart"
API_TRAVEL_CLAIM = "/v2/activity/growth/buddy/travel/claim"
API_BUDDY_INFO = "/v2/activity/growth/buddy/info"
API_TASKS = "/v2/activity/growth/tasks"
API_TASK_ACCEPT = "/v2/activity/growth/tasks/accept"
API_TASK_CLAIM = "/v2/activity/growth/tasks/{task_code}/claim"

REQUEST_TIMEOUT = 20
MAX_RETRY = 3
THROTTLE_SECONDS = 0.5

CST = timezone(timedelta(hours=8))


def now_cst():
    return datetime.now(CST)


def today_str():
    return now_cst().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 输出 / 日志
# ---------------------------------------------------------------------------

_VERBOSE = False
_LOG_LINES = []


def _safe_encode_env():
    """Windows 控制台可能是 GBK,统一切成 UTF-8,避免中文输出炸掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg, level="INFO"):
    line = f"[{level}] {msg}"
    _LOG_LINES.append(line)
    if _VERBOSE or level in ("WARN", "ERROR"):
        print(line)


def out(msg=""):
    print(msg)


def mask(s, keep=6):
    """凭据掩码:任何日志/report/push 中都不允许出现 token 原文。"""
    if not s:
        return ""
    s = str(s)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-4:]}(len={len(s)})"


# ---------------------------------------------------------------------------
# 凭据:读取本机 WorkBuddy 登录会话
# ---------------------------------------------------------------------------

def _localappdata():
    if platform.system() == "Windows":
        return os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support")
    return os.path.expanduser("~/.local/share")


def _appdata():
    if platform.system() == "Windows":
        return os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support")
    return os.path.expanduser("~/.config")


def candidate_auth_files():
    """按优先级返回候选会话文件路径(客户端明文会话,只读)。"""
    env = os.environ.get("BUDDY_CREDITS_AUTH_FILE")
    cands = []
    if env:
        cands.append(env)

    la = _localappdata()
    ra = _appdata()
    home = os.path.expanduser("~")

    # 实测命中: %LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
    for root in (la, ra, os.path.join(home, ".config"), os.path.join(home, "Library", "Application Support")):
        for mid in ("CodeBuddyExtension", "WorkBuddyExtension", "@genie", "WorkBuddy", "CodeBuddy"):
            for tail in (
                os.path.join("Data", "Public", "auth"),
                os.path.join("Data", "Public", "Auth"),
                "auth",
                os.path.join("workbuddy-desktop", "Data", "Public", "auth"),
            ):
                cands.append(os.path.join(root, mid, tail))
    # 已知文件名直接给全路径,优先级最高
    for root in (la, ra):
        cands.insert(0, os.path.join(root, "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"))
    return cands


def _is_encrypted_field(v):
    """桌面端 5.9.3+ 可能把敏感字段包成 {"$wbEncrypted":1,"envelope":...}。"""
    return isinstance(v, dict) and v.get("$wbEncrypted") == 1


def _parse_session_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    account = data.get("account") or {}
    auth = data.get("auth") or {}
    token = auth.get("accessToken")
    uid = account.get("uid")
    if not token or not uid:
        return None
    encrypted = _is_encrypted_field(token)
    nickname = account.get("nickname")
    if _is_encrypted_field(nickname):
        nickname = ""
    if not isinstance(token, str):
        token = ""
    return {
        "path": path,
        "uid": uid,
        "nickname": nickname or account.get("name") or "",
        "access_token": token,
        "encrypted": encrypted,
        "refresh_token": auth.get("refreshToken") or "",
        "expires_at": auth.get("expiresAt") or 0,
        "refresh_expires_at": auth.get("refreshExpiresAt") or 0,
        "enterprise_id": account.get("enterpriseId") or "",
        "domain": auth.get("domain") or "",
        "raw": data,
    }


def load_session():
    """发现并解析本机登录会话。找不到返回 None。"""
    tried = []
    # 环境变量可指向文件,也可指向目录
    env = os.environ.get("BUDDY_CREDITS_AUTH_FILE")
    if env and os.path.isfile(env):
        try:
            s = _parse_session_file(env)
            if s:
                return s, [env]
        except Exception as e:
            log(f"解析 BUDDY_CREDITS_AUTH_FILE 失败: {e}", "WARN")
        return None, [env]
    found = []
    seen = set()
    for d in candidate_auth_files():
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        for n in names:
            if not n.endswith(".info"):
                continue
            p = os.path.join(d, n)
            if p in seen:
                continue
            seen.add(p)
            tried.append(p)
            try:
                s = _parse_session_file(p)
            except Exception:
                continue
            if s:
                found.append((p, s))
    if found:
        found.sort(key=lambda it: _session_rank(it[1], it[0]))
        best = found[0][1]
        # 桌面端 5.9.3+ 字段级加密: 借桌面端自身的加密模块解密
        if best.get("encrypted") or not best.get("access_token"):
            fixed, err = decrypt_session_via_desktop(best)
            if fixed:
                return fixed, tried
            best["decrypt_error"] = err
        return best, tried
    return None, tried


# ---------------------------------------------------------------------------
# 桌面端加密会话的解密通道
# ---------------------------------------------------------------------------

def find_desktop_exe():
    """定位 WorkBuddy.exe（解密必须借用它的 Electron 内部绑定）。"""
    env = os.environ.get("BUDDY_CREDITS_DESKTOP_EXE")
    cands = [env] if env else []
    home = os.path.expanduser("~")
    la = _localappdata()
    cands += [
        r"D:\WorkBuddy\WorkBuddy.exe",
        os.path.join(la, "Programs", "WorkBuddy", "WorkBuddy.exe"),
        os.path.join(la, "WorkBuddy", "WorkBuddy.exe"),
        r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
        r"C:\Program Files (x86)\WorkBuddy\WorkBuddy.exe",
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    # 退路: 从正在运行的进程命令行里找
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='WorkBuddy.exe'", "get", "ExecutablePath", "/value"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        for line in out.splitlines():
            if line.startswith("ExecutablePath=") and line.strip().endswith("WorkBuddy.exe"):
                p = line.split("=", 1)[1].strip()
                if os.path.isfile(p):
                    return p
    except Exception:
        pass
    return None


def decrypt_session_via_desktop(session):
    """用桌面端 Electron 二进制解出 accessToken。

    返回 (session_dict or None, error_str)。
    """
    info_path = session.get("path") or ""
    exe = find_desktop_exe()
    if not exe:
        return None, "未找到 WorkBuddy.exe（可设环境变量 BUDDY_CREDITS_DESKTOP_EXE）"
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wb_token.js")
    if not os.path.isfile(script):
        return None, f"缺少解密脚本: {script}"

    fd, tok_path = tempfile.mkstemp(prefix="buddy_tok_", suffix=".txt")
    os.close(fd)
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.run(
            [exe, script, "--exe", exe, "--info", info_path, "--out", tok_path],
            capture_output=True, text=True, timeout=70, env=env,
        )
        line = ""
        for ln in (proc.stdout or "").splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                line = ln
        if not line:
            tail = (proc.stderr or "").strip().splitlines()
            return None, "解密进程无输出" + (f": {tail[-1]}" if tail else "")
        data = json.loads(line)
        if not data.get("ok"):
            return None, f"{data.get('stage')}: {data.get('error')}"
        with open(tok_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            return None, "解密结果为空"
        fixed = dict(session)
        fixed["access_token"] = token
        fixed["encrypted"] = False
        fixed["decrypted_via"] = "desktop"
        fixed["uid"] = data.get("uid") or session.get("uid")
        fixed["domain"] = data.get("domain") or session.get("domain")
        fixed["expires_at"] = data.get("expiresAt") or session.get("expires_at") or 0
        nick = data.get("nickname")
        if isinstance(nick, str) and nick:
            fixed["nickname"] = nick
        return fixed, ""
    except subprocess.TimeoutExpired:
        return None, "解密超时"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        try:
            os.remove(tok_path)
        except OSError:
            pass


def _session_rank(s, path):
    """给候选会话排序:domain 命中官方域名优先,其次文件名精确命中。

    同名 auth 目录下常同时存在 workbuddy-desktop.info(copilot.tencent.com) 与
    workbuddy-desktop-ai.info(www.workbuddy.ai)。后者调用 /v2/* 会被 APISIX
    网关直接打回 401 Authorization Required,必须让前者优先,否则 doctor 全红。
    """
    dom = (s.get("domain") or "").lower()
    rank = 0
    for ep in ENDPOINTS:
        host = ep.split("//", 1)[-1].strip("/").lower()
        if dom == host or dom.endswith("." + host):
            rank = 2
            break
    if rank == 0 and dom and not dom.endswith(".ai"):
        rank = 1
    name = os.path.basename(path).lower()
    return (-rank, 0 if name == "workbuddy-desktop.info" else 1, name)


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message, code=None, http_status=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.request_id = request_id


class NetworkUnreachable(Exception):
    pass


class Client:
    def __init__(self, session):
        self.session = session
        self.endpoint = ENDPOINTS[0]
        self._last_call = 0.0
        self.opener = urllib.request.build_opener()
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if not proxy:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self.opener = urllib.request.build_opener()

    def _headers(self):
        h = {
            "Authorization": "Bearer " + self.session["access_token"],
            "X-User-Id": self.session["uid"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{SKILL_NAME}/{VERSION}",
        }
        if self.session.get("enterprise_id"):
            h["X-Enterprise-Id"] = self.session["enterprise_id"]
            h["X-Tenant-Id"] = self.session["enterprise_id"]
        return h

    def _throttle(self):
        delta = time.time() - self._last_call
        if delta < THROTTLE_SECONDS:
            time.sleep(THROTTLE_SECONDS - delta)
        self._last_call = time.time()

    def request(self, path, method="GET", body=None, allow_fail=False):
        """调用接口,失败自动切换备用 endpoint 并重试。"""
        last_err = None
        for ep_idx, ep in enumerate(ENDPOINTS):
            for attempt in range(MAX_RETRY):
                self._throttle()
                url = ep + path
                data = json.dumps(body if body is not None else {}).encode("utf-8")
                req = urllib.request.Request(url, data=data, method=method)
                for k, v in self._headers().items():
                    req.add_header(k, v)
                try:
                    with self.opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                        raw = resp.read().decode("utf-8", "replace")
                    self.endpoint = ep
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        raise ApiError("响应不是合法 JSON", http_status=resp.status)
                    log(f"{method} {path} -> {resp.status} code={payload.get('code')} rid={payload.get('requestId')}", "DEBUG")
                    code = payload.get("code")
                    if code not in (0, None):
                        if allow_fail:
                            return {"ok": False, "code": code, "msg": payload.get("msg", ""),
                                    "data": payload.get("data"), "request_id": payload.get("requestId")}
                        raise ApiError(payload.get("msg") or f"业务错误 code={code}",
                                       code=code, http_status=resp.status,
                                       request_id=payload.get("requestId"))
                    return {"ok": True, "code": 0, "msg": payload.get("msg", ""),
                            "data": payload.get("data"), "request_id": payload.get("requestId")}
                except urllib.error.HTTPError as e:
                    raw = ""
                    try:
                        raw = e.read().decode("utf-8", "replace")
                    except Exception:
                        pass
                    # 404 = 路由不存在,换 endpoint 没意义,但保留尝试
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = {}
                    last_err = ApiError(payload.get("msg") or raw[:200] or str(e),
                                        code=payload.get("code"),
                                        http_status=e.code,
                                        request_id=payload.get("requestId"))
                    if e.code in (401, 403):
                        raise last_err
                    if e.code == 404:
                        break  # 换下一个 endpoint
                except (urllib.error.URLError, OSError) as e:
                    last_err = NetworkUnreachable(str(e))
                # 退避
                time.sleep(min(2 ** attempt, 5))
        if isinstance(last_err, NetworkUnreachable):
            raise last_err
        if last_err:
            raise last_err
        raise NetworkUnreachable("未知网络错误")


# ---------------------------------------------------------------------------
# 业务模块
# ---------------------------------------------------------------------------

def mod_checkin(client, dry_run=False):
    """每日签到:先读状态,未签才领。天然幂等。"""
    r = client.request(API_CHECKIN_STATUS, "POST", {})
    d = r.get("data") or {}
    res = {
        "module": "checkin",
        "active": bool(d.get("active")),
        "already_checked": bool(d.get("today_checked_in")),
        "streak_days": d.get("streak_days", 0),
        "daily_credit": d.get("daily_credit", 0),
        "today_credit": d.get("today_credit", 0),
        "total_credits": d.get("total_credits", 0),
        "week_checkin_days": d.get("week_checkin_days", 0),
        "theme_name": d.get("theme_name", ""),
        "season": d.get("season"),
        "streak_bonus_credit": d.get("streak_bonus_credit", 0),
        "credits_gained": 0,
        "action": "skipped",
        "message": "",
    }
    if not res["active"]:
        res["message"] = "签到活动未开启"
        return res
    if res["already_checked"]:
        res["message"] = f"今日已签到,连签 {res['streak_days']} 天"
        return res
    if dry_run:
        res["message"] = f"未签到(试运行,可领 {res['daily_credit']} 积分)"
        return res

    r2 = client.request(API_CHECKIN_CLAIM, "POST", {}, allow_fail=True)
    if r2.get("ok"):
        data = r2.get("data") or {}
        got = data.get("credit") or data.get("credits") or data.get("reward_credit") or res["daily_credit"]
        res["credits_gained"] = int(got or 0)
        res["action"] = "claimed"
        res["already_checked"] = True
        res["streak_days"] = (res["streak_days"] or 0) + 1
        res["message"] = f"签到成功,获得 {res['credits_gained']} 积分,连签 {res['streak_days']} 天"
    else:
        # 业务拒绝(已签到/活动结束):回读一次状态确认,不误报
        back = client.request(API_CHECKIN_STATUS, "POST", {})
        bd = back.get("data") or {}
        if bd.get("today_checked_in"):
            res["already_checked"] = True
            res["streak_days"] = bd.get("streak_days", res["streak_days"])
            res["message"] = f"今日已签到(接口回读确认),连签 {res['streak_days']} 天"
        else:
            res["message"] = f"签到失败:{r2.get('msg')}"
            res["error"] = r2.get("msg")
    return res


def _pick_location(locations, strategy="max_reward"):
    if not locations:
        return None
    if strategy.startswith("fixed:"):
        try:
            lid = int(strategy.split(":", 1)[1])
        except ValueError:
            lid = None
        if lid is not None:
            for l in locations:
                if l.get("id") == lid:
                    return l
    if strategy == "random":
        import random
        return random.choice(locations)
    # 默认:奖励上限最高
    return sorted(locations, key=lambda l: (l.get("reward_credit_max", 0), -l.get("duration_hours_min", 0)), reverse=True)[0]


def mod_travel(client, dry_run=False, wait=False, strategy="max_reward", max_rounds=8,
               wait_cap_seconds=4 * 3600, max_total_seconds=6 * 3600):
    """派喵喵旅行:idle->depart, arrived->claim, traveling->等待。

    max_rounds 只统计「实际动作」(depart/claim) 次数,等待不消耗预算。
    """
    res = {
        "module": "travel",
        "rounds": [],
        "credits_gained": 0,
        "letters": [],
        "state": "",
        "message": "",
        "waiting_for": None,
        "next_trip": None,
    }

    cfg = client.request(API_TRAVEL_CONFIG, "GET", allow_fail=True)
    locations = ((cfg.get("data") or {}).get("locations") or []) if cfg.get("ok") else []
    res["locations"] = [{"id": l.get("id"), "name": l.get("name"), "code": l.get("code")} for l in locations]

    st = client.request(API_TRAVEL_STATUS, "GET", allow_fail=True)
    if not st.get("ok"):
        res["message"] = f"读取旅行状态失败:{st.get('msg')}"
        res["error"] = st.get("msg")
        return res
    d = st.get("data") or {}
    state = d.get("state", "")
    res["state"] = state

    actions = 0
    deadline = time.time() + max_total_seconds
    while actions < max_rounds and time.time() < deadline:
        # 1) 已到达 -> 领取
        if state == "arrived":
            if dry_run:
                actions += 1
                res["rounds"].append({"action": "claim(dry-run)", "credit": d.get("reward_credit", 0)})
                res["message"] = f"已到达,可领取 {d.get('reward_credit', 0)} 积分(试运行未领取)"
                # 试运行下把状态推进为 idle 以便展示后续动作,但不发请求
                state = "idle"
                d = {}
                continue
            c = client.request(API_TRAVEL_CLAIM, "POST", {}, allow_fail=True)
            if c.get("ok"):
                cd = c.get("data") or {}
                credit = int(cd.get("reward_credit") or 0)
                letter = cd.get("letter") or {}
                actions += 1
                res["credits_gained"] += credit
                res["rounds"].append({"action": "claim", "credit": credit,
                                      "location": (d.get("location") or {}).get("name", "")})
                if letter.get("text"):
                    res["letters"].append({
                        "location": (d.get("location") or {}).get("name", ""),
                        "text": letter.get("text"),
                        "guide_text": letter.get("guide_text", ""),
                    })
                state = (cd.get("state") or "idle")
            else:
                res["message"] = f"领取旅行奖励失败:{c.get('msg')}"
                res["error"] = c.get("msg")
                break
            continue

        # 2) 旅行中 -> 等待到达
        if state == "traveling":
            arrive_at = d.get("arrive_at") or 0
            server_now = d.get("server_now") or int(time.time())
            remain = max(0, int(arrive_at - server_now))
            res["waiting_for"] = {"remain_seconds": remain,
                                  "location": (d.get("location") or {}).get("name", "")}
            if not wait or dry_run:
                h, m = divmod(remain // 60, 60)
                res["message"] = f"Buddy 正在 {(d.get('location') or {}).get('name', '途中')} 旅行,还需 {h} 小时 {m} 分钟到达"
                return res
            if remain > wait_cap_seconds:
                res["message"] = f"等待时间过长({remain}s),本次不阻塞等待"
                return res
            log(f"等待旅行到达,剩余 {remain}s", "INFO")
            time.sleep(min(remain + 3, wait_cap_seconds))
            st = client.request(API_TRAVEL_STATUS, "GET", allow_fail=True)
            d = st.get("data") or {}
            state = d.get("state", "")
            res["state"] = state
            continue

        # 3) 空闲 -> 出发
        if state == "idle":
            if d.get("daily_limit_reached"):
                res["message"] = "今日旅行次数已达上限"
                return res
            loc = _pick_location(locations, strategy)
            if not loc:
                res["message"] = "未获取到旅行地点配置"
                return res
            if dry_run:
                res["rounds"].append({"action": "depart(dry-run)", "location": loc.get("name")})
                res["message"] = f"可派 Buddy 前往「{loc.get('name')}」(试运行未出发)"
                return res
            dep = client.request(API_TRAVEL_DEPART, "POST", {"location_id": loc.get("id")}, allow_fail=True)
            if dep.get("ok"):
                dd = dep.get("data") or {}
                actions += 1
                state = dd.get("state", "traveling")
                res["state"] = state
                res["rounds"].append({"action": "depart", "location": loc.get("name"),
                                      "duration_hours": dd.get("duration_hours")})
                if not wait:
                    arrive = dd.get("arrive_at") or 0
                    remain = max(0, int(arrive - (dd.get("server_now") or time.time())))
                    h, m = divmod(remain // 60, 60)
                    res["message"] = f"已派 Buddy 前往「{loc.get('name')}」,预计 {h} 小时 {m} 分钟后到达并带回明信片"
                    # 供日报渲染:收了明信片后紧接着再出发时,要把「下一站 + 剩余时间」一并报出来
                    res["next_trip"] = {
                        "location": loc.get("name") or "",
                        "remain_seconds": remain,
                        "duration_hours": dd.get("duration_hours"),
                    }
                    return res
                d = dd
                continue
            else:
                msg = dep.get("msg") or ""
                if "already traveling" in msg or dep.get("code") == 403:
                    st = client.request(API_TRAVEL_STATUS, "GET", allow_fail=True)
                    d = st.get("data") or {}
                    state = d.get("state", "")
                    continue
                res["message"] = f"派 Buddy 出发失败:{msg}"
                res["error"] = msg
                return res

        # 未知状态
        res["message"] = f"未知旅行状态:{state}"
        break

    if not res["message"]:
        res["message"] = f"本轮完成 {len(res['rounds'])} 个动作,共获得 {res['credits_gained']} 积分"
    return res


def mod_buddy(client):
    """成长计划 Buddy 信息(只读)。"""
    r = client.request(API_BUDDY_INFO, "GET", allow_fail=True)
    if not r.get("ok"):
        return {"module": "buddy", "ok": False, "message": f"读取 Buddy 信息失败:{r.get('msg')}"}
    b = (r.get("data") or {}).get("buddy") or {}
    return {
        "module": "buddy",
        "ok": True,
        "name": b.get("name", ""),
        "personality": b.get("personality", ""),
        "rarity": b.get("rarity", ""),
        "instance_id": b.get("instance_id"),
        "soul_desc": (b.get("soul_desc") or "")[:80],
    }


def mod_tasks(client, dry_run=False, accept_new=False):
    """成长计划任务:只读进度 + 领取已完成奖励。不伪造完成。"""
    r = client.request(API_TASKS, "GET", allow_fail=True)
    res = {
        "module": "tasks",
        "total": 0,
        "claimed": [],
        "pending_accept": [],
        "in_progress": [],
        "claimable_credits": 0,
        "credits_gained": 0,
        "accepted": [],
        "message": "",
    }
    if not r.get("ok"):
        res["message"] = f"读取任务列表失败:{r.get('msg')}"
        res["error"] = r.get("msg")
        return res

    tasks = (r.get("data") or {}).get("tasks") or []
    res["total"] = len(tasks)

    to_accept = []
    to_claim = []
    for t in tasks:
        code = t.get("task_code")
        status = t.get("accept_status")
        prog = t.get("progress") or {}
        credit = int(t.get("reward_credit") or 0)
        item = {"task_code": code, "title": t.get("title", ""), "credit": credit}
        if status == "claimed":
            res["claimed"].append(item)
        elif status == "not_accepted":
            if not t.get("locked"):
                to_accept.append(code)
            res["pending_accept"].append(item)
        else:
            # accepted / completed / 未来新增状态：一律按「是否达标」判定
            # 注意：服务端对已完成任务返回 status="completed"（而非 accepted），
            # 早期版本只认 accepted，导致已完成的奖励被整批漏领，这里必须兜住。
            cur = prog.get("current") or 0
            tgt = prog.get("target") or 0
            done = (status == "completed") or (tgt and cur >= tgt)
            if done:
                to_claim.append((code, credit, t.get("title", "")))
                res["claimable_credits"] += credit
            else:
                item["progress"] = "{}/{}".format(cur, tgt)
                res["in_progress"].append(item)

    # 接受未接受的任务(不消耗任何东西,只是挂载任务)
    if accept_new and to_accept and not dry_run:
        a = client.request(API_TASK_ACCEPT, "POST", {"task_codes": to_accept}, allow_fail=True)
        if a.get("ok"):
            res["accepted"] = [x.get("task_code") for x in (a.get("data") or {}).get("results", [])]
        else:
            res["message"] = f"接受任务失败:{a.get('msg')}"
    elif accept_new and to_accept and dry_run:
        res["accepted"] = [f"{c}(dry-run)" for c in to_accept]

    # 领取已完成任务的奖励
    for code, credit, title in to_claim:
        if dry_run:
            res["credits_gained"] += credit
            res["claimed"].append({"task_code": code, "title": title, "credit": credit, "dry_run": True})
            continue
        c = client.request(API_TASK_CLAIM.format(task_code=code), "POST", {}, allow_fail=True)
        if c.get("ok"):
            data = c.get("data") or {}
            got = int(data.get("credit") or credit or 0)
            res["credits_gained"] += got
            res["claimed"].append({"task_code": code, "title": title, "credit": got})
        else:
            log(f"领取任务奖励失败 {code}: {c.get('msg')}", "WARN")
            res.setdefault("errors", []).append({"task_code": code, "msg": c.get("msg")})

    if not res["message"]:
        if res["credits_gained"]:
            res["message"] = f"领取任务奖励 +{res['credits_gained']} 积分"
        elif res["claimable_credits"]:
            res["message"] = f"有 {res['claimable_credits']} 积分待领(试运行未领取)"
        else:
            res["message"] = f"无待领奖励,{len(res['in_progress'])} 个任务进行中"
    return res


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def build_report(results, session, started_at):
    checkin = next((r for r in results if r.get("module") == "checkin"), {})
    travel = next((r for r in results if r.get("module") == "travel"), {})
    tasks = next((r for r in results if r.get("module") == "tasks"), {})
    buddy = next((r for r in results if r.get("module") == "buddy"), {})

    gained = int(checkin.get("credits_gained", 0)) + int(travel.get("credits_gained", 0)) + int(tasks.get("credits_gained", 0))
    dry = any(r.get("dry_run") for r in results)

    todos = []
    for t in tasks.get("in_progress", []):
        todos.append(f"{t['title']}({t.get('progress','-')}) +{t['credit']}")
    for t in tasks.get("pending_accept", [])[:3]:
        todos.append(f"{t['title']} 未接受 +{t['credit']}")

    errors = []
    for r in results:
        if r.get("error"):
            errors.append(f"{r.get('module')}: {r['error']}")

    return {
        "skill": SKILL_NAME,
        "version": VERSION,
        "date": today_str(),
        "generated_at": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": round(time.time() - started_at, 2),
        "dry_run": dry,
        "account": {
            "uid": session.get("uid", ""),
            "nickname": session.get("nickname", ""),
        },
        "checkin": checkin,
        "travel": travel,
        "tasks": tasks,
        "buddy": buddy,
        "summary": {
            "credits_gained_today": gained,
            "total_checkin_credits": checkin.get("total_credits", 0),
            "streak_days": checkin.get("streak_days", 0),
            "errors": errors,
            "todos": todos,
        },
    }


def render_text(report):
    s = report["summary"]
    c = report.get("checkin") or {}
    t = report.get("travel") or {}
    k = report.get("tasks") or {}
    b = report.get("buddy") or {}

    L = []
    L.append(f"Buddy 加油站日报 · {report['date']}")
    L.append("─" * 42)
    if report.get("dry_run"):
        L.append("（试运行模式,未做任何领取）")
    if b.get("ok"):
        L.append(f"搭档:{b.get('name','')} · {b.get('rarity','')} {b.get('personality','')}")

    # 签到
    if c:
        if c.get("already_checked"):
            mark = "✅"
            detail = f"连签 {c.get('streak_days',0)} 天"
            if c.get("credits_gained"):
                detail += f",本次 +{c['credits_gained']}"
            else:
                detail += ",今日已签"
        else:
            mark = "⚠️"
            detail = c.get("message") or "未签到"
        L.append(f"{mark} 每日签到   {detail}")
        if c.get("total_credits"):
            L.append(f"            累计签到积分 {c['total_credits']}（第 {c.get('season','-')} 期 · {c.get('theme_name','')}）")

    # 旅行
    if t:
        rounds = t.get("rounds") or []
        # 「还剩多久」：优先取本次再出发的倒计时,其次取旅行中的倒计时
        nt = t.get("next_trip") or {}
        wf = t.get("waiting_for") or {}
        remain = nt.get("remain_seconds") or wf.get("remain_seconds") or 0
        eta = ""
        if remain:
            h, m_ = divmod(int(remain) // 60, 60)
            eta = f",还需 {h} 小时 {m_} 分" if h else f",还需 {m_} 分钟"
        if t.get("credits_gained"):
            locs = "、".join([r.get("location", "") for r in rounds if r.get("action") == "claim" and r.get("location")])
            line = f"🐱 派喵喵旅行 收明信片 +{t['credits_gained']}" + (f"（{locs}）" if locs else "")
            if nt.get("location"):
                line += f" → 已再出发前往「{nt['location']}」{eta}"
            L.append(line)
        elif wf:
            L.append(f"🐱 派喵喵旅行 {t.get('message','')}")
        else:
            L.append(f"🐱 派喵喵旅行 {t.get('message') or '无动作'}")
        for lt in t.get("letters", [])[:2]:
            first = (lt.get("text") or "").strip().split("\n")[0]
            L.append(f"   📮 来自{lt.get('location','')}:{first[:38]}...")

    # 任务
    if k:
        if k.get("credits_gained"):
            L.append(f"📋 成长计划   领取奖励 +{k['credits_gained']}")
        elif k.get("claimable_credits"):
            L.append(f"📋 成长计划   待领 +{k['claimable_credits']}（试运行未领）")
        else:
            L.append(f"📋 成长计划   {k.get('message','')}")
        if k.get("accepted"):
            if report.get("dry_run"):
                L.append(f"   待接受新任务 {len(k['accepted'])} 个（试运行未执行）")
            else:
                L.append(f"   已接受新任务 {len(k['accepted'])} 个")
        if k.get("in_progress"):
            L.append(f"   进行中 {len(k['in_progress'])} 个（需在客户端实际使用才能达成）")

    L.append("─" * 42)
    L.append(f"💰 今日合计  +{s['credits_gained_today']} 积分")
    if s.get("todos"):
        L.append("🎯 待办提醒")
        for x in s["todos"][:5]:
            L.append(f"   · {x}")
    if s.get("errors"):
        L.append("❗ 异常")
        for x in s["errors"][:5]:
            L.append(f"   · {x}")
    return "\n".join(L)


def render_markdown(report):
    s = report["summary"]
    c = report.get("checkin") or {}
    t = report.get("travel") or {}
    k = report.get("tasks") or {}
    L = [
        f"## Buddy 加油站日报 · {report['date']}",
        "",
        "| 模块 | 结果 | 积分 |",
        "| --- | --- | --- |",
    ]
    ci = "已签" if c.get("already_checked") else (c.get("message") or "-")
    L.append(f"| 每日签到 | {ci}（连签 {c.get('streak_days',0)} 天） | +{c.get('credits_gained',0)} |")
    L.append(f"| 派喵喵旅行 | {t.get('message','-')} | +{t.get('credits_gained',0)} |")
    L.append(f"| 成长计划任务 | {k.get('message','-')} | +{k.get('credits_gained',0)} |")
    L.append(f"| **合计** | | **+{s['credits_gained_today']}** |")
    L.append("")
    if s.get("todos"):
        L.append("### 待办")
        for x in s["todos"][:8]:
            L.append(f"- {x}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 状态落盘
# ---------------------------------------------------------------------------

def state_dir():
    env = os.environ.get("BUDDY_CREDITS_STATE_DIR")
    if env:
        return env
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
    elif platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.path.expanduser("~/.local/share")
    return os.path.join(base, SKILL_NAME)


def load_state():
    p = os.path.join(state_dir(), "state.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state, report=None):
    d = state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        state["updated_at"] = now_cst().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        if report is not None:
            with open(os.path.join(d, "last-report.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        # 追加日志(自动裁剪 30 天)
        logf = os.path.join(d, "buddy-credits.log")
        with open(logf, "a", encoding="utf-8") as f:
            f.write(f"\n===== {now_cst().strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            f.write("\n".join(_LOG_LINES) + "\n")
    except Exception as e:
        log(f"状态落盘失败:{e}", "WARN")


def append_daily_ledger(report):
    """审计账本:按天记录已领积分,便于回溯。"""
    d = state_dir()
    p = os.path.join(d, "ledger.jsonl")
    try:
        os.makedirs(d, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "date": report["date"],
                "at": report["generated_at"],
                "gained": report["summary"]["credits_gained_today"],
                "checkin": (report.get("checkin") or {}).get("credits_gained", 0),
                "travel": (report.get("travel") or {}).get("credits_gained", 0),
                "tasks": (report.get("tasks") or {}).get("credits_gained", 0),
                "dry_run": report.get("dry_run", False),
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"账本写入失败:{e}", "WARN")


# ---------------------------------------------------------------------------
# IM 推送
# ---------------------------------------------------------------------------

def config_path():
    env = os.environ.get("BUDDY_CREDITS_CONFIG")
    if env:
        return env
    return os.path.join(state_dir(), "config.json")


def load_config():
    p = config_path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"配置文件解析失败:{e}", "WARN")
        return {}


def _expand(v):
    """支持 ${ENV_VAR} 占位,避免明文密钥进配置文件。"""
    if not isinstance(v, str):
        return v
    import re
    def rep(m):
        return os.environ.get(m.group(1), "")
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", rep, v)


def _post_json(url, payload, headers=None, timeout=15):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")[:400]


def _push_feishu(cfg, title, text):
    url = _expand(cfg.get("webhook_url") or cfg.get("url"))
    if not url:
        return False, "缺少 webhook_url"
    payload = {"msg_type": "text", "content": {"text": f"{title}\n\n{text}"}}
    secret = _expand(cfg.get("secret"))
    if secret:
        ts = str(int(time.time()))
        digest = hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
        payload["timestamp"] = ts
        payload["sign"] = base64.b64encode(digest).decode()
    st, body = _post_json(url, payload)
    ok = st == 200 and '"code":0' in body.replace(" ", "") or st == 200
    return ok, f"HTTP {st} {body[:120]}"



def _push_wecom(cfg, title, text):
    url = _expand(cfg.get("webhook_url") or cfg.get("url"))
    if not url:
        return False, "缺少 webhook_url"
    payload = {"msgtype": "text", "text": {"content": f"{title}\n\n{text}"}}
    st, body = _post_json(url, payload)
    return st == 200, f"HTTP {st} {body[:120]}"


def _push_dingtalk(cfg, title, text):
    url = _expand(cfg.get("webhook_url") or cfg.get("url"))
    if not url:
        return False, "缺少 webhook_url"
    secret = _expand(cfg.get("secret"))
    if secret:
        ts = str(round(time.time() * 1000))
        sign = urllib.parse.quote_plus(
            base64.b64encode(hmac.new(secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"), hashlib.sha256).digest())
        )
        url = f"{url}&timestamp={ts}&sign={sign}"
    payload = {"msgtype": "text", "text": {"content": f"{title}\n\n{text}"}}
    st, body = _post_json(url, payload)
    return st == 200, f"HTTP {st} {body[:120]}"


def _push_telegram(cfg, title, text):
    token = _expand(cfg.get("bot_token"))
    chat = _expand(cfg.get("chat_id"))
    if not token or not chat:
        return False, "缺少 bot_token 或 chat_id"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": f"{title}\n\n{text}"}
    st, body = _post_json(url, payload)
    return st == 200, f"HTTP {st} {body[:120]}"


def _push_serverchan(cfg, title, text):
    key = _expand(cfg.get("send_key"))
    if not key:
        return False, "缺少 send_key"
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": title, "desp": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status == 200, f"HTTP {r.status}"


def _push_custom(cfg, title, text):
    url = _expand(cfg.get("url"))
    if not url:
        return False, "缺少 url"
    method = (cfg.get("method") or "POST").upper()
    headers = {k: _expand(v) for k, v in (cfg.get("headers") or {}).items()}
    tpl = cfg.get("body_template") or '{"title":"{title}","text":"{text}"}'
    body = tpl.replace("{title}", title).replace("{text}", text.replace("\n", "\\n").replace('"', '\\"'))
    try:
        payload = json.loads(body)
        st, resp = _post_json(url, payload, headers)
    except ValueError:
        data = body.encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", cfg.get("content_type", "application/json"))
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as r:
            st, resp = r.status, r.read().decode("utf-8", "replace")[:200]
    return 200 <= st < 300, f"HTTP {st} {resp[:120]}"


PUSHERS = {
    "feishu": ("飞书", _push_feishu),
    "wecom": ("企业微信", _push_wecom),
    "dingtalk": ("钉钉", _push_dingtalk),
    "telegram": ("Telegram", _push_telegram),
    "serverchan": ("Server酱", _push_serverchan),
}


def notify(report, only=None):
    cfg = load_config()
    notify_cfg = cfg.get("notify") or cfg
    title = f"Buddy 加油站日报 · {report['date']} · +{report['summary']['credits_gained_today']} 积分"
    text = render_text(report)
    results = []

    for key, (label, fn) in PUSHERS.items():
        if only and key not in only:
            continue
        block = notify_cfg.get(key)
        if not block or block is True:
            continue
        if isinstance(block, list):
            items = block
        else:
            items = [block]
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            try:
                ok, info = fn(item, title, text)
            except Exception as e:
                ok, info = False, str(e)
            results.append({"channel": f"{label}#{i+1}", "ok": ok, "info": info})
            log(f"推送 {label}#{i+1}: {'成功' if ok else '失败'} {info}", "INFO" if ok else "WARN")

    for i, item in enumerate(notify_cfg.get("custom", []) if isinstance(notify_cfg.get("custom"), list) else []):
        if only and "custom" not in only:
            continue
        if not item.get("enabled", True):
            continue
        try:
            ok, info = _push_custom(item, title, text)
        except Exception as e:
            ok, info = False, str(e)
        results.append({"channel": f"custom#{i+1}", "ok": ok, "info": info})
        log(f"推送 custom#{i+1}: {'成功' if ok else '失败'} {info}", "INFO" if ok else "WARN")

    return results


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_doctor(args):
    _safe_encode_env()
    out("Buddy 积分助手 · 本机状态自检")
    out("─" * 42)
    session, tried = load_session()
    if not session:
        out("❌ 未找到有效的本机登录会话")
        out("   已探测路径:")
        for p in tried[:8]:
            out(f"   · {p}")
        out("   请在 WorkBuddy 桌面端登录一次,或设置环境变量 BUDDY_CREDITS_AUTH_FILE 指向会话文件")
        return 2

    out(f"✅ 会话文件: {session['path']}")
    out(f"   用户: {session['nickname'] or '(未命名)'}  uid={session['uid']}")
    out(f"   accessToken: {mask(session['access_token'])}")
    if session["expires_at"]:
        exp = datetime.fromtimestamp(session["expires_at"] / 1000, CST)
        remain = (exp - now_cst()).total_seconds()
        flag = "✅" if remain > 3600 else ("⚠️" if remain > 0 else "❌")
        out(f"   {flag} 过期时间: {exp.strftime('%Y-%m-%d %H:%M:%S')}（剩余 {int(remain//3600)} 小时）")
    if session["enterprise_id"]:
        out(f"   企业账号: {session['enterprise_id']}")

    client = Client(session)
    ok_count = 0
    for label, path, method in [
        ("签到状态", API_CHECKIN_STATUS, "POST"),
        ("旅行状态", API_TRAVEL_STATUS, "GET"),
        ("Buddy 信息", API_BUDDY_INFO, "GET"),
        ("任务列表", API_TASKS, "GET"),
    ]:
        try:
            r = client.request(path, method, {}, allow_fail=True)
            flag = "✅" if r.get("ok") else "❌"
            out(f"   {flag} {label}: {path} -> code={r.get('code')} {r.get('msg') or ''}")
            if r.get("ok"):
                ok_count += 1
        except Exception as e:
            out(f"   ❌ {label}: {e}")
    out("─" * 42)
    out(f"接口连通性: {ok_count}/4")
    cfg = load_config()
    ncfg = cfg.get("notify") or cfg
    channels = [k for k in PUSHERS if isinstance(ncfg.get(k), (dict, list))]
    if ncfg.get("custom"):
        channels.append("custom")
    out(f"推送通道: {', '.join(channels) if channels else '未配置（' + config_path() + '）'}")
    return 0 if ok_count >= 3 else 1


def cmd_run(args):
    global _VERBOSE
    _VERBOSE = args.verbose
    _safe_encode_env()
    started = time.time()

    session, tried = load_session()
    if not session:
        out("❌ 未找到本机登录会话,请先在 WorkBuddy 桌面端登录。")
        out(f"   已探测: {len(tried)} 个路径")
        return 2
    if session["expires_at"] and session["expires_at"] / 1000 < time.time():
        out("❌ 登录凭据已过期,请打开 WorkBuddy 桌面端重新登录以刷新会话。")
        return 2
    if session.get("encrypted"):
        out("❌ 会话文件里的 accessToken 已被桌面端加密($wbEncrypted),解密通道也未成功。")
        out(f"   文件: {session['path']}")
        if session.get("decrypt_error"):
            out(f"   解密失败原因: {session['decrypt_error']}")
        out("   处置: 确认 WorkBuddy 桌面端已登录并保持安装路径可访问"
            "(或设 BUDDY_CREDITS_DESKTOP_EXE 指向 WorkBuddy.exe);重新登录本身不能解决加密。")
        return 2

    client = Client(session)
    results = []
    failures = 0
    only = (args.only or "").strip().lower()

    # 签到
    if only in ("", "checkin", "signin", "签到"):
        try:
            r = mod_checkin(client, dry_run=args.dry_run)
            r["dry_run"] = args.dry_run
            results.append(r)
        except NetworkUnreachable as e:
            out(f"❌ 网络不可达:{e}")
            return 3
        except ApiError as e:
            results.append({"module": "checkin", "error": str(e), "message": f"签到失败:{e}"})
            failures += 1

    # 旅行
    if only in ("", "travel", "旅行"):
        try:
            r = mod_travel(client, dry_run=args.dry_run, wait=args.wait,
                           strategy=args.location or "max_reward")
            r["dry_run"] = args.dry_run
            results.append(r)
        except NetworkUnreachable as e:
            out(f"❌ 网络不可达:{e}")
            return 3
        except ApiError as e:
            results.append({"module": "travel", "error": str(e), "message": f"旅行失败:{e}"})
            failures += 1

    # Buddy 信息
    if only in ("", "buddy"):
        try:
            results.append(mod_buddy(client))
        except Exception as e:
            results.append({"module": "buddy", "ok": False, "error": str(e)})

    # 任务
    if only in ("", "tasks", "task", "任务"):
        try:
            r = mod_tasks(client, dry_run=args.dry_run, accept_new=args.accept_tasks)
            r["dry_run"] = args.dry_run
            results.append(r)
        except NetworkUnreachable as e:
            out(f"❌ 网络不可达:{e}")
            return 3
        except ApiError as e:
            results.append({"module": "tasks", "error": str(e), "message": f"任务失败:{e}"})
            failures += 1

    report = build_report(results, session, started)
    state = load_state()
    state["last_run"] = report["generated_at"]
    state["last_date"] = report["date"]
    if not args.dry_run:
        state["last_gained"] = report["summary"]["credits_gained_today"]
    save_state(state, report)
    if not args.dry_run:
        append_daily_ledger(report)

    if args.json:
        out(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.markdown:
        out(render_markdown(report))
    else:
        out()
        out(render_text(report))
        out()

    push_results = []
    if args.notify:
        push_results = notify(report, only=args.channel)
        if push_results:
            out("推送结果:")
            for p in push_results:
                out(f"   {'✅' if p['ok'] else '❌'} {p['channel']}: {p['info']}")
    report["push"] = push_results

    if failures:
        return 1
    return 0


def cmd_notify(args):
    _safe_encode_env()
    p = os.path.join(state_dir(), "last-report.json")
    if not os.path.isfile(p):
        out("❌ 尚无最近报告,请先运行 run")
        return 1
    with open(p, "r", encoding="utf-8") as f:
        report = json.load(f)
    res = notify(report, only=args.channel)
    for r in res:
        out(f"   {'✅' if r['ok'] else '❌'} {r['channel']}: {r['info']}")
    if not res:
        out("未配置任何推送通道:" + config_path())
        return 1
    return 0 if all(r["ok"] for r in res) else 1


def main():
    parser = argparse.ArgumentParser(
        prog="buddy_credits.py",
        description="Buddy 积分助手:自动签到 / 派喵喵旅行 / 领任务积分,并推送日报到 IM",
    )
    parser.add_argument("--version", action="version", version=f"{SKILL_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="跑完整闭环")
    p_run.add_argument("--dry-run", action="store_true", help="只读巡检,不做任何领取")
    p_run.add_argument("--notify", action="store_true", help="推送日报到配置的 IM")
    p_run.add_argument("--channel", nargs="*", help="只推送指定通道,如 feishu wecom")
    p_run.add_argument("--wait", action="store_true", help="阻塞等待旅行到达,拿满当天旅行次数")
    p_run.add_argument("--only", default="", help="只跑指定模块:checkin/travel/tasks/buddy")
    p_run.add_argument("--accept-tasks", dest="accept_tasks", action="store_true", default=True,
                       help="自动接受未接受的任务（默认开启，接受后才会开始计算进度）")
    p_run.add_argument("--no-accept-tasks", dest="accept_tasks", action="store_false",
                       help="不自动接受新任务")
    p_run.add_argument("--location", default="max_reward", help="地点策略:max_reward/random/fixed:<id>")
    p_run.add_argument("--json", action="store_true", help="输出 JSON")
    p_run.add_argument("--markdown", action="store_true", help="输出 Markdown")
    p_run.add_argument("--verbose", "-v", action="store_true", help="打印调试日志")
    p_run.set_defaults(func=cmd_run)

    p_doc = sub.add_parser("doctor", help="检查本机登录态与接口连通性")
    p_doc.add_argument("--verbose", "-v", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    p_n = sub.add_parser("notify", help="只推送最近一次报告")
    p_n.add_argument("--channel", nargs="*")
    p_n.set_defaults(func=cmd_notify)

    # 允许省略 run
    argv = sys.argv[1:]
    if argv and argv[0] not in ("run", "doctor", "notify", "-h", "--help", "--version"):
        argv = ["run"] + argv
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
