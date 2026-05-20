# PR-14 功能规划: Gitea Webhook 模式

**PR 编号**: PR-14
**规划日期**: 2026-05-19
**基础分支**: `feat/pr-14-webhook` (stacked on `feat/pr-13-dashboard-mode`)
**预估工作量**: **4-6 人日** (12 子任务, 中等强度, 大量复用 PR-12/13 现成 helper)
**关键风险数**: 9 条

---

## 1. 一句话目标

把 PR-12 的 30s 轮询升级为 **Gitea webhook 内网推送主路径 + 5min 轮询兜底**, 实现秒级响应 + 双保险, court / dashboard 两模式同时受益, 不动 PR-12/13 核心代码.

---

## 2. 架构图

```
                              主路径 (秒级)
  ┌─────────────────────┐    HTTP POST          ┌──────────────────────────┐
  │  git.k2lab.ai       │ ─────────────────────▶│  gitea-webhook-receiver  │
  │  (Gitea 内网)        │   X-Gitea-Signature   │  (aiohttp :8765 独立进程) │
  │  issue opened/      │   X-Gitea-Delivery    │                          │
  │  assigned/edited    │   X-Gitea-Event       │  1. HMAC-SHA256 校验      │
  └─────────────────────┘                       │  2. event 过滤            │
                                                │  3. assignee 过滤         │
                                                │  4. 写盘 (原子, 200 ack) │
                                                └──────────┬───────────────┘
                                                           │ atomic write
                                                           ▼
                                       ~/.agent-court/gitea-watcher/
                                              pending-webhook/
                                                <ts>-<delivery>.json
                                                           │
                                                           │ 优先读
                                                           ▼
  ┌────────────────────┐                       ┌──────────────────────────┐
  │  Polling 兜底分支   │                       │  GiteaWatcher.run_once() │
  │  每 300s (env       │ ────list_issues()────▶│                          │
  │  WATCHER_POLL_      │                       │  ① _consume_pending_     │
  │  INTERVAL 可覆盖)   │                       │     webhook()  (新增)     │
  └────────────────────┘                       │  ② list_assigned_issues()│
                                                │  ③ _diff()                │
                                                │  ④ _dispatch_shenli()    │
                                                │  ⑤ _apply_decision()     │
                                                │     / _apply_decision_   │
                                                │       dashboard()        │
                                                └──────────┬───────────────┘
                                                           │
                            ┌──────────────────────────────┴──────┐
                            ▼                                     ▼
                  ┌──────────────────┐                 ┌──────────────────┐
                  │  court 模式       │                 │  dashboard 模式   │
                  │  申诉/陪审/判决    │                 │  ImReplyRouter   │
                  └──────────────────┘                 └──────────────────┘
```

**关键解耦**: receiver **只校验 + 落盘**, 不调 `GiteaClient` / 不跑 shenli; watcher **只读盘 + 合并队列**, 不知道 webhook 怎么来的. 任一端故障时另一端自然降级.

---

## 3. 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | **双保险架构 = webhook 主推 + 5min 轮询兜底** | webhook 可能因网络抖动/launchd 重启/secret 不一致丢失; 轮询 5min 兜底保证最终一致, 但平均响应从 15s (30s/2) 降到秒级 |
| D2 | **独立进程 (新 receiver + watcher 分进程)** | 防 watcher 重启/卡死时丢 webhook; aiohttp 长连可能阻塞 watcher loop; launchd 单独管理重启策略 |
| D3 | **receiver 不调 GiteaClient / shenli, 只落盘** | (a) receiver 必须 < 200ms 返回 200, 否则 Gitea 重试风暴; (b) 解耦后 receiver 跑挂不影响 watcher 轮询兜底; (c) pending 文件天然提供 replay/audit |
| D4 | **方案 A: 内网部署, 不走 ngrok / Cloudflare Tunnel** | git.k2lab.ai 已在 K2Lab 内网, Mac 也在内网, 直接 HTTP 即可; 公网暴露会引入 TLS + 域名 + 防 DDoS 一堆负担, 当前规模没必要 |
| D5 | **Keychain `service=git.k2lab.ai-webhook` (跟 PR-12 严格分离)** | PR-12 oauth2 token 用 `service=git.k2lab.ai`; macOS keychain 同 host 多条目可能在 `security find-generic-password -s` 时出错; 用不同 service 名彻底隔离, **已踩过这个坑** |
| D6 | **WATCHER_POLL_INTERVAL 默认 300s (5min)** | webhook 是主路径后, 轮询纯兜底; 太频繁浪费 Gitea API quota; PR-12 用户老 env `=30` 还能继续用, 不破坏向后兼容 |
| D7 | **stacked on PR-13, 不改 PR-12/13 核心代码** | PR-14 = 新增 receiver + watcher 加一个入口钩子, _diff/_dispatch/_apply_decision 完全不动; 评审窗口集中在新文件 |
| D8 | **内部错误一律 ack 200** | Gitea webhook 失败重试机制激进, 5xx 会风暴; 内部异常打 stderr 日志, 200 ack 让 Gitea 不重试, 失败 webhook 自然走 5min 轮询兜底 |
| D9 | **pending-webhook 处理后归档到 .processed/, 不原地删** | 方便 audit + 故障 replay; rm 不可逆, 归档可手工 mv 回 retry |

---

## 4. WBS 任务分解

### 任务总览

| ID | 子任务 | 模块 | 估算 | 依赖 |
|----|--------|------|------|------|
| T-14-01 | webhook_secret 模块 | M2 | 小 | - |
| T-14-02 | receiver aiohttp app 骨架 | M1 | 中 | T-14-01 |
| T-14-03 | HMAC 签名校验中间件 | M1 | 小 | T-14-02 |
| T-14-04 | event/assignee 过滤 + 落盘 | M1 | 中 | T-14-02 |
| T-14-05 | receiver CLI 入口 + main | M1 | 小 | T-14-02 |
| T-14-06 | bash launcher + plist | M3 | 中 | T-14-05 |
| T-14-07 | watcher `_consume_pending_webhook()` | M4 | 中 | T-14-04 |
| T-14-08 | watcher poll_interval 默认 300s | M4 | 小 | - |
| T-14-09 | receiver 单元测试 (7 case) | M5 | 中 | T-14-04 |
| T-14-10 | watcher 集成单测 (3 case) | M5 | 小 | T-14-07 |
| T-14-INT | e2e_local_webhook.sh | M5 | 小 | T-14-06 |
| T-14-DOC-01 | docs/webhook-mode.md | M5 | 中 | T-14-06 |
| T-14-DOC-02 | README + 模式对比表 | M5 | 小 | T-14-DOC-01 |

总计: **13 任务** (含 INT/DOC), 估算 4-6 人日.

---

### M2 · Keychain Secret Helper

#### T-14-01 · webhook_secret 模块

- **目标**: 提供 `get_webhook_secret()` 的统一获取接口, fallback 链 keychain → env
- **产出文件**:
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/webhook_secret.py` (新建)
- **关键接口**:
  ```python
  class WebhookSecretMissing(Exception): ...

  def get_webhook_secret() -> str:
      """优先 macOS Keychain (service=git.k2lab.ai-webhook, account=webhook-secret),
      fallback env K2LAB_WEBHOOK_SECRET. 都没有抛 WebhookSecretMissing."""

  def _read_from_keychain() -> str | None:
      """subprocess: security find-generic-password -s git.k2lab.ai-webhook -a webhook-secret -w"""

  # CLI
  if __name__ == "__main__":
      # python -m webhook_secret check
      # 打印 "secret found: <length> chars, source: keychain|env"
  ```
- **复用**: 模仿 `mcp/court-mcp/gitea_credentials.py` 的 `_read_from_keychain` 思路, **但 service name 必须不同**
- **依赖**: 无
- **验收命令**:
  ```bash
  cd /Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp
  K2LAB_WEBHOOK_SECRET=testsecret python -m webhook_secret check
  # 期望输出: "secret found: 10 chars, source: env"
  ```
- **估算**: 小 (<2h)

---

### M1 · HTTP Receiver Python 模块

#### T-14-02 · receiver aiohttp app 骨架

- **目标**: 搭起 aiohttp web.Application, 路由表 + 启动函数
- **产出文件**:
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_webhook_receiver.py` (新建)
- **HTTP 路由表**:

  | 方法 | 路径 | 处理函数 | 响应 |
  |------|------|----------|------|
  | POST | `/gitea/webhook` | `handle_webhook` | 200 always |
  | GET | `/healthz` | `handle_health` | 200 `{"status":"ok","pending":<count>}` |

- **关键接口**:
  ```python
  def build_app(secret: str, pending_dir: Path, gitea_username: str) -> web.Application: ...

  async def handle_health(request: web.Request) -> web.Response: ...

  async def handle_webhook(request: web.Request) -> web.Response: ...

  def main() -> int: ...  # CLI 入口
  ```
- **依赖**: T-14-01 (用 `get_webhook_secret`)
- **复用**: 模仿 `mcp/court-mcp/yiguan_daemon.py` 的 aiohttp app 启动模式 (但 yiguan 用 ed25519, 不能直接复制签名逻辑)
- **验收命令**:
  ```bash
  cd /Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp
  K2LAB_WEBHOOK_SECRET=test python -m gitea_webhook_receiver --port 8765 &
  sleep 1
  curl -sf http://localhost:8765/healthz | python -m json.tool
  kill %1
  ```
- **估算**: 中 (半天)

#### T-14-03 · HMAC-SHA256 签名校验

- **目标**: 在 `handle_webhook` 入口校验 `X-Gitea-Signature` (裸 hex, 无 `sha256=` 前缀)
- **产出文件**: 续 `gitea_webhook_receiver.py`
- **关键逻辑**:
  ```python
  def verify_signature(body: bytes, secret: str, header_sig: str) -> bool:
      """Gitea: hex(hmac_sha256(body, secret)) - NO 'sha256=' prefix"""
      expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
      return hmac.compare_digest(expected, header_sig.strip())
  ```
- **错误处理**: 签名不通过 → 200 ack + stderr `"signature mismatch from <ip>"` (**不返 403 防 Gitea 重试**)
- **依赖**: T-14-02
- **验收**: 单测覆盖正/负样例 (T-14-09 第 1, 2 case)
- **估算**: 小 (<2h)

#### T-14-04 · event + assignee 过滤 + 原子落盘

- **目标**: 解析 payload, 过滤 event 类型 + assignee, 写 pending-webhook/
- **过滤规则**:
  - `X-Gitea-Event == "issues"` (其他事件 200 + drop, stderr 日志)
  - `payload.action in {"opened", "assigned", "edited", "reopened"}` (其他 200 + drop)
  - `gitea_username in [a.login for a in payload.issue.assignees]` (不含自己 200 + drop)
- **落盘 JSON schema**:
  ```json
  {
      "schema_version": 1,
      "received_at": "2026-05-19T12:34:56Z",
      "delivery_id": "abc-uuid-from-X-Gitea-Delivery",
      "action": "assigned",
      "issue": { ...原 payload.issue 完整对象... },
      "repository": { "full_name": "K2Lab/agent-court", ... },
      "sender": { "login": "..." }
  }
  ```
- **文件命名**: `pending-webhook/<unix_ts_int>-<delivery_id>.json`
- **原子写**: 复用 `gitea_watcher.py` 里的 tempfile + os.replace 模式
- **依赖**: T-14-02
- **验收**:
  ```bash
  # 单元测试 T-14-09 第 3-6 case 覆盖
  ```
- **估算**: 中 (半天)

#### T-14-05 · receiver CLI 入口 + 内部异常兜底

- **目标**: `python -m gitea_webhook_receiver` 可启动, 所有未捕获异常 → 200 ack + log
- **CLI 用法**:
  ```bash
  python -m gitea_webhook_receiver --port 8765 --bind 0.0.0.0 [--court-root ~/.agent-court]
  ```
- **CLI 参数**:

  | 参数 | 默认 | 说明 |
  |------|------|------|
  | `--port` | 8765 | 监听端口 |
  | `--bind` | 0.0.0.0 | 绑定地址 (内网部署需要 0.0.0.0 才能收外部 IP) |
  | `--court-root` | env `COURT_ROOT` or `~/.agent-court` | 状态目录 |

- **启动检查**:
  - 调 `get_webhook_secret()`, 缺失则 stderr 报错 + 退出码 2
  - 调 `GiteaClient().whoami()` 拿 `username`, 失败退出码 3
  - mkdir pending-webhook + pending-webhook/.processed
- **全局异常 middleware**:
  ```python
  @web.middleware
  async def error_to_200(request, handler):
      try:
          return await handler(request)
      except Exception as exc:
          print(f"[ERROR] {request.path}: {exc}", file=sys.stderr)
          return web.Response(status=200, text="error logged")
  ```
- **依赖**: T-14-01 ~ T-14-04
- **验收**:
  ```bash
  K2LAB_WEBHOOK_SECRET=test python -m gitea_webhook_receiver --port 8765 &
  PID=$!
  sleep 1
  # 故意发坏的 payload, 应该 200 不 crash
  curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8765/gitea/webhook \
       -H "X-Gitea-Signature: deadbeef" -d '{"bad":"json"'
  # 期望: 200
  kill $PID
  ```
- **估算**: 小 (<2h)

---

### M3 · bin launcher + launchd plist

#### T-14-06 · bin/gitea-webhook-receiver + plist 模板

- **目标**: bash 启动器 + launchd 配置, 模仿 PR-12 `bin/gitea-watcher`
- **产出文件**:
  - `/Users/wjx/Desktop/K2Work/agent-court/bin/gitea-webhook-receiver` (新建, chmod +x)
  - `/Users/wjx/Desktop/K2Work/agent-court/docs/launchd/ai.k2lab.gitea-webhook-receiver.plist.template` (新建)
- **launcher 子命令表**:

  | 子命令 | 行为 |
  |--------|------|
  | `install [--port N]` | 检查 secret → sed plist 模板 → cp 到 `~/Library/LaunchAgents/` → `launchctl bootstrap` |
  | `start` | `launchctl kickstart -k gui/$UID/ai.k2lab.gitea-webhook-receiver` |
  | `stop` | `launchctl bootout gui/$UID/ai.k2lab.gitea-webhook-receiver` |
  | `status` | `launchctl print` 解析 PID + 状态 |
  | `logs [-f]` | `tail [-f] ~/Library/Logs/gitea-webhook-receiver.{out,err}.log` |
  | `foreground` | 本进程跑 `python -m gitea_webhook_receiver --port $PORT` (调试用) |
  | `help` | 用法 |

- **install 前置检查 (硬性)**:
  ```bash
  if ! security find-generic-password -s git.k2lab.ai-webhook -a webhook-secret -w >/dev/null 2>&1; then
      cat <<EOF
  错误: keychain 缺少 webhook secret
  请先执行:
    security add-generic-password -s git.k2lab.ai-webhook -a webhook-secret -w '<your-secret>'
  EOF
      exit 1
  fi
  ```
- **plist 模板占位符**:

  | 占位符 | 替换为 |
  |--------|--------|
  | `__PORT__` | --port 参数 (默认 8765) |
  | `__HOME__` | `$HOME` |
  | `__REPO_ROOT__` | `$(cd $(dirname $0)/.. && pwd)` |
  | `__COURT_ROOT__` | `${COURT_ROOT:-$HOME/.agent-court}` |
  | `__PYTHON__` | `$(which python3)` |

- **plist 关键字段**:
  ```xml
  <key>Label</key><string>ai.k2lab.gitea-webhook-receiver</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>__REPO_ROOT__/mcp/court-mcp</string>
  <key>StandardOutPath</key><string>__HOME__/Library/Logs/gitea-webhook-receiver.out.log</string>
  <key>StandardErrorPath</key><string>__HOME__/Library/Logs/gitea-webhook-receiver.err.log</string>
  <key>ProgramArguments</key>
  <array>
      <string>__PYTHON__</string><string>-m</string><string>gitea_webhook_receiver</string>
      <string>--port</string><string>__PORT__</string>
      <string>--bind</string><string>0.0.0.0</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
      <key>COURT_ROOT</key><string>__COURT_ROOT__</string>
  </dict>
  ```
  **绝对不在 plist 写 secret**, 全走 keychain.
- **复用**: 100% 模仿 `docs/launchd/ai.k2lab.gitea-watcher.plist.template` 和 `bin/gitea-watcher`, sed 替换套路一致
- **依赖**: T-14-05
- **验收**:
  ```bash
  cd /Users/wjx/Desktop/K2Work/agent-court
  ./bin/gitea-webhook-receiver foreground --port 8766 &
  sleep 2
  curl -sf http://localhost:8766/healthz
  kill %1
  ```
- **估算**: 中 (半天)

---

### M4 · Watcher 集成

#### T-14-07 · `_consume_pending_webhook()` 钩子

- **目标**: 在 `GiteaWatcher.run_once()` 开头读 pending-webhook/, 合并进 queued 队列
- **修改文件**:
  - `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_watcher.py`
- **集成点** (run_once 内):
  ```python
  def run_once(self) -> dict[str, int]:
      self._ensure_dirs()
      self._ensure_pending_webhook_dirs()        # 新增
      with self._state_lock():
          seen = self._load_json(self.seen_path, {})
          webhook_issues = self._consume_pending_webhook()  # 新增, 返回 list[dict]
          polling_issues = self.client.list_assigned_issues(state="open")
          all_issues = self._merge_sources(webhook_issues, polling_issues)  # 新增, dedup by (repo, number)
          queued = self._merge_retry_candidates(all_issues, seen)
          # ... 后续 _diff / _dispatch_shenli / _apply_decision 完全不动
  ```
- **新增方法签名**:
  ```python
  def _ensure_pending_webhook_dirs(self) -> None:
      """mkdir pending-webhook + pending-webhook/.processed"""

  def _consume_pending_webhook(self) -> list[dict]:
      """读 pending-webhook/*.json (按文件名排序), 解出 issue 字段, 处理完归档到 .processed/.
      解析失败的文件归档到 .processed/<name>.invalid, stderr 日志."""

  def _merge_sources(self, webhook_items: list[dict], polling_items: list[dict]) -> list[dict]:
      """webhook 优先 (是事件时刻最新状态), 但 polling 可能比 webhook 更新 (因为 polling 取的是
      实时 GET /issues). 规则: 同 (repo, number) → 取 updated_at 较晚的那个."""
  ```
- **seen_entry 字段扩展** (`_build_seen_entry` 函数):
  ```python
  entry = {
      "repo": ...,
      "number": ...,
      "updated_at": ...,
      "last_action": ...,
      "court_project": ...,
      "shenli_run_at": ...,
      "source": "webhook" | "polling",   # 新增
      "webhook_event_id": "<delivery-uuid>" | None,  # 新增, 来源 webhook 时填
  }
  ```
- **归档逻辑**:
  - 处理完: `pending-webhook/123-uuid.json` → `pending-webhook/.processed/123-uuid.json`
  - 解析失败: → `pending-webhook/.processed/123-uuid.json.invalid`
  - **绝不 rm**, 防误删 + 方便 audit
- **复用**:
  - `mcp/court-mcp/seen_state.py` 锁 (state_lock 已在 PR-13 抽出)
  - `_atomic_write_json` (PR-12)
  - 现有 `_diff()` / `_dispatch_shenli()` / `_apply_decision()` / `_apply_decision_dashboard()` 全部不动
- **依赖**: T-14-04 (落盘格式)
- **验收**: 单测 T-14-10
- **估算**: 中 (半天)

#### T-14-08 · 默认轮询周期改 300s

- **目标**: PR-12 原默认 30s, PR-14 改 300s (5min); env `WATCHER_POLL_INTERVAL` 仍可覆盖, 向后兼容
- **修改文件**: `mcp/court-mcp/gitea_watcher.py`
- **改动点**:
  ```python
  # GiteaWatcher.__init__ 默认参数
  def __init__(self, poll_interval: int = 300, ...):   # 30 → 300

  # CLI args (loop 子命令)
  parser.add_argument("--poll-interval", type=int,
                      default=int(os.environ.get("WATCHER_POLL_INTERVAL", "300")))
  ```
- **依赖**: 无
- **风险**: PR-12 用户 launchd plist 里如果 hardcode 了 `--poll-interval 30`, 不受影响; 没 hardcode 的 → 自动跟新默认走, 想保持 30s 可设 env
- **验收**:
  ```bash
  cd /Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp
  python -c "from gitea_watcher import GiteaWatcher; w = GiteaWatcher(); print(w.poll_interval)"
  # 期望: 300
  WATCHER_POLL_INTERVAL=60 python -c "import os; from gitea_watcher import GiteaWatcher; w = GiteaWatcher(poll_interval=int(os.environ['WATCHER_POLL_INTERVAL'])); print(w.poll_interval)"
  # 期望: 60
  ```
- **估算**: 小 (<2h)

---

### M5 · 测试 + 文档

#### T-14-09 · receiver 单元测试 (7 case)

- **产出文件**: `/Users/wjx/Desktop/K2Work/agent-court/tests/test_gitea_webhook_receiver.py`
- **用例清单**:

  | # | 用例名 | 验证点 |
  |---|--------|--------|
  | 1 | `test_valid_signature_writes_pending` | 正确签名 + opened event → 落盘成功 |
  | 2 | `test_invalid_signature_ack_200_no_write` | 签名错 → 200 但不落盘 |
  | 3 | `test_missing_signature_ack_200` | 无 header → 200 + drop |
  | 4 | `test_non_issues_event_dropped` | `X-Gitea-Event=push` → 200 + drop |
  | 5 | `test_unrelated_action_dropped` | `action=labeled` → 200 + drop |
  | 6 | `test_assignee_filter_dropped` | issue.assignees 不含本人 → 200 + drop |
  | 7 | `test_internal_exception_returns_200` | 模拟 disk full → 200 + stderr 日志 |
  | (8) | `test_healthz_endpoint` | GET /healthz 返回 pending count |

- **复用**: aiohttp `pytest-aiohttp` 测试 client, tmpdir 当 pending-webhook
- **依赖**: T-14-04
- **验收**:
  ```bash
  cd /Users/wjx/Desktop/K2Work/agent-court
  pytest tests/test_gitea_webhook_receiver.py -v
  # 期望: 7 passed (or 8 with healthz)
  ```
- **估算**: 中 (半天)

#### T-14-10 · watcher 集成单测 (3 case)

- **产出文件**: `/Users/wjx/Desktop/K2Work/agent-court/tests/test_watcher_webhook_consumption.py`
- **用例清单**:

  | # | 用例名 | 验证点 |
  |---|--------|--------|
  | 1 | `test_consume_pending_webhook_merges_into_queue` | 提前手工写 `<ts>-<uuid>.json` → run_once() 读到 + dispatch + 归档 .processed/ |
  | 2 | `test_dedup_when_same_issue_in_webhook_and_polling` | webhook 和 list_assigned_issues 都返回 issue#42, 但 polling updated_at 更晚 → 取 polling |
  | 3 | `test_seen_entry_records_webhook_source` | webhook 触发的 issue → seen[key].source == "webhook" + webhook_event_id 非 None |

- **mock**: `GiteaClient.list_assigned_issues` 用 stub
- **依赖**: T-14-07
- **验收**:
  ```bash
  pytest tests/test_watcher_webhook_consumption.py -v
  ```
- **估算**: 小 (<2h)

#### T-14-INT · e2e_local_webhook.sh

- **产出文件**: `/Users/wjx/Desktop/K2Work/agent-court/tests/webhook/e2e_local_webhook.sh`
- **脚本流程**:
  ```bash
  #!/usr/bin/env bash
  set -e

  # 1. 启 receiver foreground (后台)
  cd "$(dirname "$0")/../.."
  K2LAB_WEBHOOK_SECRET=e2e-test-secret \
      ./bin/gitea-webhook-receiver foreground --port 18765 &
  RECEIVER_PID=$!
  trap "kill $RECEIVER_PID 2>/dev/null" EXIT
  sleep 2

  # 2. 健康检查
  curl -sf http://localhost:18765/healthz >/dev/null
  echo "OK: healthz"

  # 3. 构造 payload + 签名
  PAYLOAD='{"action":"assigned","issue":{"id":1,"number":42,"assignees":[{"login":"testuser"}],"updated_at":"2026-05-19T12:00:00Z"},"repository":{"full_name":"K2Lab/test"},"sender":{"login":"bot"}}'
  SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "e2e-test-secret" | awk '{print $2}')

  # 4. POST
  HTTP_CODE=$(curl -s -o /tmp/resp.txt -w "%{http_code}" \
      -X POST http://localhost:18765/gitea/webhook \
      -H "Content-Type: application/json" \
      -H "X-Gitea-Event: issues" \
      -H "X-Gitea-Delivery: e2e-test-uuid-001" \
      -H "X-Gitea-Signature: $SIG" \
      -d "$PAYLOAD")
  [ "$HTTP_CODE" = "200" ] || { echo "FAIL: expected 200, got $HTTP_CODE"; exit 1; }

  # 5. 验落盘
  sleep 0.5
  test -f ~/.agent-court/gitea-watcher/pending-webhook/*-e2e-test-uuid-001.json
  echo "OK: pending file written"

  # 6. 验签名错路径
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST http://localhost:18765/gitea/webhook \
      -H "X-Gitea-Signature: deadbeef" \
      -H "X-Gitea-Event: issues" \
      -H "X-Gitea-Delivery: e2e-test-uuid-002" \
      -d "$PAYLOAD")
  [ "$HTTP_CODE" = "200" ] || { echo "FAIL: expected 200 even on bad sig"; exit 1; }
  echo "OK: bad signature still acks 200"

  echo "ALL E2E PASS"
  ```
- **依赖**: T-14-06
- **验收**: 脚本本身退出码 0
- **估算**: 小 (<2h)

#### T-14-DOC-01 · docs/webhook-mode.md

- **产出文件**: `/Users/wjx/Desktop/K2Work/agent-court/docs/webhook-mode.md`
- **章节结构**:

  | 节 | 内容 |
  |----|------|
  | 1. 概述 | 双保险架构 + 跟 court/dashboard 模式的关系 |
  | 2. 部署前提 | macOS only, 内网部署, keychain secret 准备 |
  | 3. 安装步骤 | (a) keychain 写 secret (b) `./bin/gitea-webhook-receiver install` (c) `./bin/gitea-webhook-receiver start` (d) 健康检查 |
  | 4. Gitea Web 端配置 | 截图+步骤: Settings → Webhooks → Add Webhook → URL `http://<mac内网IP>:8765/gitea/webhook` + Secret + Events 选 Issues 三项 |
  | 5. 内网 IP 获取 | `ipconfig getifaddr en0` |
  | 6. 防火墙放行 | System Settings → Network → Firewall → Add Python.app + 允许 incoming on :8765 |
  | 7. 验证 webhook | Gitea Web → Test Delivery → 应在 receiver 日志看到 |
  | 8. 排障 | 表格: 症状 → 检查命令 → 修复 |
  | 9. 安全提示 | secret 不要进 git / plist / env 文件; 用 keychain |
  | 10. 卸载 | `./bin/gitea-webhook-receiver stop` + `launchctl bootout` + `security delete-generic-password` |

- **依赖**: T-14-06
- **验收**: 自己按文档走一遍能装上能收到 webhook
- **估算**: 中 (半天)

#### T-14-DOC-02 · README + 模式对比表

- **产出文件**: `/Users/wjx/Desktop/K2Work/agent-court/README.md` (修改, 加 "webhook 模式 (PR-14)" 小节)
- **关键内容 - 模式对比表**:

  | 模式 | PR | 响应时延 | 部署复杂度 | 适用场景 |
  |------|----|---------|----------|---------|
  | court (轮询) | PR-12 | 平均 15s | 低 (launchd 一个进程) | 单人 dev, 简单试用 |
  | dashboard | PR-13 | 平均 15s | 中 (+ IM router) | 多人协作, 双向派单 |
  | webhook | PR-14 | < 1s + 5min 兜底 | 高 (+ receiver 进程, Gitea 配置) | 生产环境, 内网部署 |

- **交叉引用**:
  - 修改 `/Users/wjx/Desktop/K2Work/agent-court/.claude/plan/dashboard-mode.md` 末尾加一句 "PR-14 webhook 模式进一步把响应降到秒级, 见 webhook-mode.md"
- **依赖**: T-14-DOC-01
- **估算**: 小 (<2h)

---

## 5. 依赖图 (Critical Path)

```
         ┌──────────────────────────────────────────────────────────┐
         │  T-14-01 webhook_secret                                  │
         │  (Critical Path 起点)                                     │
         └──────────────┬───────────────────────────────────────────┘
                        ▼
         ┌──────────────────────────────────────────────────────────┐
         │  T-14-02 aiohttp app 骨架                                 │
         └──────────────┬───────────────────────────────────────────┘
                        ▼
        ┌───────────────────┐
        ▼                   ▼
  T-14-03 签名校验      T-14-04 过滤+落盘
                            ▼
                      T-14-05 CLI 入口
                            ▼
        ┌───────────────────┼─────────────────────┐
        ▼                   ▼                     ▼
  T-14-06 launcher    T-14-07 watcher       T-14-09 receiver 单测
        │              consume钩子                 │
        ▼                   ▼                     │
  T-14-INT e2e        T-14-10 watcher 单测        │
        │                   │                     │
        └─────────┬─────────┴─────────────────────┘
                  ▼
            T-14-DOC-01
                  ▼
            T-14-DOC-02

  T-14-08 poll_interval=300 (独立, 可并行做任何时候)
```

**Critical Path**: T-14-01 → T-14-02 → T-14-04 → T-14-05 → T-14-06 → T-14-07 → T-14-INT → T-14-DOC

**可并行**:
- T-14-03 ∥ T-14-04 (都基于 T-14-02)
- T-14-08 (孤立, 任何时候做)
- T-14-09 ∥ T-14-10 (单测互不相关)

---

## 6. PR 拆分建议

| 选项 | 范围 | 评价 |
|------|------|------|
| **A. 一次性 PR-14** | M1+M2+M3+M4+M5 全打包 | 推荐. 总改动量约 800-1000 行 (含测试 + 文档), 跟 PR-12/13 体量相当. 评审一次过, 用户配置 Gitea webhook 一次到位. |
| **B. 拆 PR-14a + PR-14b** | 14a: M1+M2+M4+M5 基础 (foreground 跑); 14b: M3 launchd | 不推荐. 14a 只能 foreground 跑没生产价值, 14b 独立又太薄 (就 1 个 bash + 1 个 plist). 强行拆增加 stacked 复杂度. |

**结论**: 一次性 PR-14, 但 commit 内部按 M1→M5 顺序分批 (方便 review 时按顺序看). 建议 commits 数量约 8-10 个, 每个 commit 对应 1 个 T-14-XX.

---

## 7. 风险清单

| # | 风险 | 影响 | 缓解 |
|---|------|------|------|
| R1 | **secret 泄漏到 plist / git** | 高 (任何能 cat plist 的本机进程都能伪造 webhook) | plist 100% 不含 secret, 启动时 `get_webhook_secret()` 读 keychain; pre-commit hook 扫 `K2LAB_WEBHOOK_SECRET=` 字面值; docs/webhook-mode.md 第 9 节专门强调 |
| R2 | **端口 8765 被占用** | 中 (receiver 启动失败但 launchd KeepAlive 会无限重试, 日志堆积) | launcher install 时 `lsof -i :8765` 预检, 占用则 abort + 提示换端口; receiver main() 启动失败 ` sys.exit(4)` 让 launchd 看到非 0 退出不无限重试 (configure `ExitTimeOut` + `ThrottleInterval`) |
| R3 | **内网 IP 漂移 (切 WiFi → IP 变)** | 高 (Gitea 配置失效, 静默丢 webhook) | docs/webhook-mode.md 给 "切网后 5min 内 5min 兜底接管" 兜底说明 + 提供 `./bin/gitea-webhook-receiver check-ip` 子命令打印当前 IP 跟 Gitea 配置对比; 长期方案 (本 PR 不做): mDNS `<mac-name>.local` |
| R4 | **Gitea 重试风暴 (receiver 返 5xx)** | 严重 (内部 bug 导致 receiver 一直 500, Gitea 短时间内重试 N 次, 日志炸) | 全局 error_to_200 middleware (T-14-05) 兜底; 单测 case 7 覆盖 disk full 等异常路径 |
| R5 | **keychain 条目跟 PR-12 oauth2 token 冲突** | 中 (security find 找错条目, 拿到 oauth2 token 当 secret 用, HMAC 永远过不了) | **service name 严格分离**: `git.k2lab.ai-webhook` ≠ `git.k2lab.ai`; T-14-01 用 `-s` 指定全名, 不用 `-l` (label) |
| R6 | **macOS 防火墙拦截 incoming** | 高 (Gitea POST 不到本机, 还看不到拒绝日志) | docs/webhook-mode.md 第 6 节专门讲: System Settings → Firewall 加 python3 入站; install 子命令最后打印 "请确认防火墙允许 :8765 入站" 提示 |
| R7 | **pending-webhook 重复消费 (watcher 没及时归档崩了)** | 中 (issue 被 dispatch 两次, 走 _diff 兜不住的话会跑两遍 shenli) | (a) 处理完立即 `os.rename` 到 .processed/ (原子); (b) `_diff()` 已有 seen 机制兜底, 同 issue 同 updated_at 不会再 dispatch; (c) 加 `seen[key].webhook_event_id` 字段, 同 delivery_id 视为已处理 |
| R8 | **webhook payload.issue vs list_assigned_issues 返回结构不一致** | 中 (字段 missing 导致 _build_seen_entry 崩) | T-14-07 `_merge_sources` 对 webhook issues 字段做 schema 校验 + 缺字段 fallback; 关键字段对比表写进 `_consume_pending_webhook` docstring; 整体策略 "polling 优先, webhook 作为新增源补充而非替换" — 同 (repo, number) 取 updated_at 较晚者 |
| R9 | **5min 兜底周期太长, 业务接受度低** | 低 (但实际 webhook 路径正常时秒级响应, 兜底纯保险) | env `WATCHER_POLL_INTERVAL` 可调; docs/webhook-mode.md 说明: 如果用户对 webhook 信任度低, 可设 60 (1min) |

---

## 8. 验收清单

PR-14 合并前必须全部通过:

```
[ ] AC-01 keychain 写入 secret + ./bin/gitea-webhook-receiver install 成功
[ ] AC-02 ./bin/gitea-webhook-receiver start 后 launchctl list 看到进程
[ ] AC-03 curl http://localhost:8765/healthz 返回 200 + JSON
[ ] AC-04 tests/test_gitea_webhook_receiver.py 7+ 用例全过
[ ] AC-05 tests/test_watcher_webhook_consumption.py 3 用例全过
[ ] AC-06 tests/webhook/e2e_local_webhook.sh 退出码 0
[ ] AC-07 完整 pytest 套件不退化 (PR-13 250 passed → PR-14 应 260+ passed)
[ ] AC-08 Gitea Web 配置 webhook + Test Delivery → receiver 日志收到 + pending-webhook/ 落盘 + watcher 下次 run_once 消费成功
```

---

## 9. 本次不做 (Out of Scope)

显式声明不做的事, 防 scope creep:

- **不做公网暴露** (ngrok / Cloudflare Tunnel / TLS 证书) — 方案 A 内网部署
- **不做 webhook 重放保护 (replay cache)** — Gitea 内部网络已可信, 且 5min 兜底自然防漏处理; 若后续公网部署再加
- **不做 issue 以外的事件** (push / pull_request / release ...) — 当前只关心 issue assignment 流程
- **不动 PR-12/13 核心代码** (`_diff` / `_dispatch_shenli` / `_apply_decision` / `_apply_decision_dashboard` 不动一行)
- **不做 admin 面板** (查 pending-webhook 队列长度 / .processed 归档量) — 走文件系统 + `./bin/gitea-webhook-receiver logs` 就够
- **不做多 Gitea host 支持** — 当前固定 git.k2lab.ai, secret 单一; 多 host 改 service name 即可但本 PR 不引入
- **不重写 `gitea_credentials.py`** — webhook secret 单独走 `webhook_secret.py`, 老 oauth2 token 路径不动
- **不引入新依赖** — aiohttp 已在 PR-7 pyproject.toml; 不加 cryptography / pyjwt 等

---

## 10. 引用 - 复用的现有代码

PR-14 直接 import 不重写的:

| 现有文件 | 复用接口 |
|---------|---------|
| `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_client.py` | `GiteaClient.whoami()` (receiver 启动时拿 username) |
| `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_credentials.py` | 参考 `_read_from_keychain` 实现模式 (但 service name 不同) |
| `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/gitea_watcher.py` | `_atomic_write_json` / `_load_json` / `_state_lock` / `_issue_key` / `_issue_repo` / `_diff` / `_dispatch_shenli` / `_apply_decision*` |
| `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/seen_state.py` (PR-13) | 共享锁 helper |
| `/Users/wjx/Desktop/K2Work/agent-court/mcp/court-mcp/yiguan_daemon.py` (PR-7) | aiohttp web.Application 启动 + middleware 模式参考 (签名算法不同, 不能直接复制) |
| `/Users/wjx/Desktop/K2Work/agent-court/bin/gitea-watcher` (PR-12) | bash launcher 子命令骨架 + sed 替换套路 |
| `/Users/wjx/Desktop/K2Work/agent-court/docs/launchd/ai.k2lab.gitea-watcher.plist.template` (PR-12) | plist 模板 100% 套用 |

不重写, 直接 import / cp 改个名 + 改 service / Label.

---

## 11. 后续优化方向 (Out of PR-14, 留 PR-15+)

- mDNS 支持 (`<mac-name>.local`) 解决内网 IP 漂移
- webhook delivery_id replay cache (公网暴露时必备)
- admin REST: `GET /admin/pending-webhook?status=processed|invalid` 查归档
- 多 Gitea host 支持 (service name 动态拼接)
- Slack / Lark 实时通知接入 (webhook 收到时同步 ping IM)
