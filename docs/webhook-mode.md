# Webhook 模式 (PR-14)

把 PR-12 的 30s 轮询升级为 **git.k2lab.ai 内网 webhook 推送主路径 + 轮询 5min 兜底** 的双保险架构。

## 跟 PR-12 (court) / PR-13 (dashboard) 的关系

| 维度 | PR-12 court (默认) | PR-13 dashboard | PR-14 webhook 增量 |
|------|--------|--------|--------|
| 派活粒度 | 一 issue 一座 court project (黑盒并行) | 一 issue 一个 Claude window (白盒) | 不变 (是上游入口, 不动 court/dashboard) |
| 触发延迟 | 30s 轮询 | 30s 轮询 | **秒级 webhook** + 5min 兜底 |
| 部署 | launchd 后台 | tmux session 前台 | + receiver 独立进程 (launchd KeepAlive) |
| 凭证 | Keychain `service=git.k2lab.ai` (oauth2) | 同左 | + Keychain `service=git.k2lab.ai-webhook` (HMAC secret) |

webhook 仅是**新增上游入口**, court 模式 / dashboard 模式核心逻辑零改动. 用户可自由选 court / dashboard 模式, 然后**叠加** webhook 加速.

## 数据流

```
┌─────────────────────────────────┐
│ git.k2lab.ai (内网 Gitea)        │
│ 配 webhook: 内网 IP:8765         │
└──────────────┬──────────────────┘
               │ POST /gitea/webhook
               │ + X-Gitea-Signature
               ▼
┌─────────────────────────────────┐
│ gitea-webhook-receiver (本机)   │
│ • aiohttp HTTP server, :8765    │
│ • 校 HMAC-SHA256                │
│ • 过滤 event=issues + assignee  │
│ • 落盘 pending-webhook/*.json   │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│ ~/.agent-court/gitea-watcher/   │
│   pending-webhook/<ts>-<uuid>.json│
└──────────────┬──────────────────┘
               │
   ┌───────────┴───────────┐
   │ watcher 每 5min 跑    │  ← polling 兜底
   │ run_once 优先消费 ↑  │
   └───────────┬───────────┘
               ▼
         _diff + _dispatch_shenli + _apply_decision*
         (复用 PR-12 + PR-13 现有路径; seen-issues 加 source=webhook)
```

## 启动步骤

### 1. 录入 webhook secret 到 macOS Keychain

```bash
# 替换 <SECRET> 为你想用的随机串 (至少 32 字符, 用 openssl rand -hex 16 生成)
security add-generic-password -s git.k2lab.ai-webhook -a webhook-secret -w <SECRET>

# 验证
cd /Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp
.venv/bin/python -m webhook_secret check    # → OK
.venv/bin/python -m webhook_secret source   # → keychain
```

**重要**: `service=git.k2lab.ai-webhook` (跟 PR-12 oauth2 token 的 `service=git.k2lab.ai` 严格分离). 否则 `git credential-osxkeychain get` 会选错条目, 导致 push 403 (user 已踩过坑).

### 2. 装 launchd 守护

```bash
cd /Users/wjx/Desktop/K2Work/agent-court
bin/gitea-webhook-receiver install --port 8765
bin/gitea-webhook-receiver start
bin/gitea-webhook-receiver status   # 看 launchd 在不在
bin/gitea-webhook-receiver logs     # 看 stderr/stdout 最近 50 行
```

或者**前台调试**:

```bash
bin/gitea-webhook-receiver foreground
# 另一个终端: curl http://localhost:8765/healthz → {"status":"ok"}
```

### 3. 取本机内网 IP

```bash
# WiFi
ipconfig getifaddr en0    # e.g. 192.168.1.42
# 有线
ipconfig getifaddr en1
```

### 4. 在 Gitea 后台配 webhook

进入 git.k2lab.ai 任一 repo → Settings → Webhooks → Add Webhook → Gitea

| 字段 | 值 |
|------|------|
| Target URL | `http://<内网IP>:8765/gitea/webhook` |
| HTTP Method | POST |
| POST Content Type | `application/json` |
| Secret | (第 1 步录入到 Keychain 的那个 SECRET) |
| Trigger On | Custom Events → **Issue Events** |
| Branch filter | * (留空) |
| Active | ✓ |

保存后点 **Test Delivery**, 应该 200 OK.

(如果有多个 repo, 每个都得加一次. 或者去组织级 webhook 一次性覆盖.)

### 5. 防火墙放行 (macOS)

System Settings → Network → Firewall → Options →
确保允许 **Python** (跑 receiver 的进程) 接收 incoming 连接.

或者关掉 Firewall (内网环境通常关).

### 6. 验证全链路

在 git.k2lab.ai 建个测试 issue, 指派给自己 → 30 秒内:

```bash
# pending-webhook 落盘
ls -la ~/.agent-court/gitea-watcher/pending-webhook/
# 等下次 watcher 跑 (5min, 想立即试可以手动)
bin/gitea-watcher --once
# 看 seen-issues 标 source=webhook
cat ~/.agent-court/gitea-watcher/seen-issues.json | grep -A2 webhook
```

## 兜底机制 (双保险)

- **webhook 收到**: receiver 立刻写 pending-webhook/, watcher 下次 run_once 处理 (court/dashboard 模式都受益)
- **webhook 丢包 / receiver 重启 / Gitea 重试失败**: watcher 每 5min 仍走 polling, 列出所有 assigned issues, 跟 seen 对比拿 new/updated, 同样路径处理

也就是说就算 receiver 整个挂了, **issue 最多延迟 5min 被处理**. webhook 只是把延迟从 5min 降到秒级.

## 状态机字段 (seen-issues.json 新增)

```json
{
  "K2Lab/moras-finder#7": {
    "repo": "K2Lab/moras-finder",
    "number": 7,
    "last_action": "GO",
    "source": "webhook",            // ← PR-14 新增: webhook | polling
    "webhook_event_id": "delivery-uuid-...",  // ← PR-14 新增 (source=webhook 时)
    ...
  }
}
```

## 排障

| 症状 | 排查 |
|------|------|
| `bin/gitea-webhook-receiver foreground` 报 `WebhookSecretMissing` | 没录 keychain secret, 跑步骤 1 |
| Gitea Web 后台 Test Delivery 返 401 | secret 不匹配; 检查 Gitea 后台的 Secret 是否跟 keychain 里的一致; receiver logs 看 "invalid signature" |
| Gitea Test Delivery 200 但 receiver 没动作 | 检查 `X-Gitea-Event`: 不是 issues 会 silent drop; `X-Gitea-Delivery` 是否带 |
| pending-webhook 落盘了但 watcher 没动 | 看 watcher 进程在不在; `bin/gitea-watcher --once` 手动触发一次; `~/Library/Logs/gitea-watcher.*.log` |
| 内网 IP 换了 (切了 WiFi) | Gitea 后台 webhook URL 要同步改; 或者用 hostname 而不是 IP |
| 端口 8765 被占 | `lsof -i :8765` 查谁占了; 用 `install --port 9876` 换个端口 |
| receiver 频繁重启 (logs 显示 KeepAlive throttle) | secret 找不到导致启动期 raise; 把 secret 录好 / 或者临时设 env `K2LAB_WEBHOOK_SECRET` |
| **keychain 取错 oauth2 token / push 403** | 严重错误: 检查是否在同一 host `git.k2lab.ai` 录了多个 username 条目. **铁律**: `git.k2lab.ai` 只留 `oauth2` push token, webhook secret 必须用**不同** service `git.k2lab.ai-webhook` |

## 安全注意

- **不公网暴露**: receiver bind `0.0.0.0` 只在内网生效, **不要做 NAT/端口转发把 8765 暴露到公网**. 没有任何重放保护, 公网暴露 = 被 DDoS 风暴.
- HMAC secret 至少 32 字符随机串
- 防火墙仅放行**已知内网 IP 段** (System Preferences → Firewall → Options)
- secret 不落盘任何 plist / .env / dotfile, 一律走 Keychain

## 内网 webhook 不行的替代方案 (留 PR-15+)

如果 git.k2lab.ai 不在你内网 (远程同事、跨机房等), 走 cloudflared tunnel 把本地 :8765 暴露成 https://xxx.trycloudflare.com:

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8765
# 拿到 URL 后填到 Gitea Web 后台 webhook 配置 URL
```

(本 PR 不实现 cloudflared 集成, 留 PR-15+.)

## 关闭 webhook 模式 (回退到 PR-12 纯轮询)

```bash
bin/gitea-webhook-receiver stop
# 把 Gitea 后台对应 repo 的 webhook 禁用 / 删除
# watcher 默认仍 5min 轮询, 想恢复 30s:
export WATCHER_POLL_INTERVAL=30
bin/gitea-watcher install --mode court  # 或 dashboard
bin/gitea-watcher start
```

## 相关文档

- PR-12: `docs/issue-driven-workflow.md` — court 模式 + 凭证 + pre-push hook
- PR-13: `docs/dashboard-mode.md` — dashboard 双通道审批
- PR-14: 本文
