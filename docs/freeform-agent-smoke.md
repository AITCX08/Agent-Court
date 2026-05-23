# Freeform Agent — 本地 E2E Smoke

> PR-19b 系列 (#42 #43 #44 #45) 全合并后, 验证 dashboard 上 "+ 启动新 Agent" 完整流程可走通的最短脚本.

## 前置

```bash
# 1. dashboard server 已起来 (本地常用方式)
cd mcp/court-mcp && .venv/bin/python -m court_dashboard_server
# 默认监听 127.0.0.1:9100; 浏览器访问 http://127.0.0.1:9100/?t=<token>

# 2. tmux 可用
which tmux

# 3. claude CLI 在 PATH 里
which claude
```

## 步骤

### Step 1 — 启动 modal

1. 浏览器进 dashboard
2. 左侧菜单点 "Agent 团队"
3. 顶部 toolbar 右上角点 "+ 启动新 Agent" (紫色按钮)
4. 弹出 modal, 标题 "启动新需求 Agent (1/4)"

### Step 2 — 填初始需求 + 起 agent

1. **任务标签**: 填一个短名字, e.g. `smoke-test`
2. **大白话需求**: 填一段实际需求, e.g.
   ```
   做个测试: 在 docs/ 下新建一个 hello.md 文件, 写一句话 "hello from freeform agent"
   ```
3. 点 **"下一步"** → POST `/api/agent/freeform-spawn`
4. 切到 Step 2 (标题 "跟 Agent 沟通 (2/4)"), 顶部显示 team_id `agent-team-xxxxxxxx`
5. monospace pre 区开始显示 tmux pane 内容 (SSE 实时推, 应当 ≤1s 看到 agent 启动输出)

### Step 3 — 跟 agent 聊澄清

1. agent 应当先调 `/req` skill 给你提问
2. 在底部 textarea 答 agent 的问题, Cmd+Enter (Mac) / Ctrl+Enter 提交
3. 来回几轮直到 agent 输出 `### REQ READY ###` 然后调 `superpowers:writing-plans` 写 plan
4. plan 写好后 agent 输出 ` ```markdown ... ``` ` 块 + `### PLAN READY ###` sentinel
5. Step 2 footer 的 **"下一步"** 按钮变可点 (变蓝亮); 没出 PLAN READY 时按钮 disabled + tooltip 提示

### Step 4 — 审 plan + 开工

1. 点 **"下一步"** → 切到 Step 3 (标题 "审计划 (3/4)")
2. pre 区显示提取出来的 plan markdown (regex 抓最后一个 ` ```markdown ``` ` 块)
3. 没问题 → 点底部绿色 **"开工"** 按钮 → 调 `sendAgentInput("/proceed")` 发字符串进 tmux
4. agent 收到 `/proceed` 后输出 `### EXECUTE START ###` → 前端自动切到 Step 4

### Step 5 — 看执行进度

1. Step 4 (标题 "Agent 执行中 (4/4)") 显示 read-only pane + 状态徽章
2. agent 每完成一个 task 输出 `### TASK DONE: <task name> ###`
3. Step 4 头部显示 **"已完成 N 个任务: <最后任务名>"** (PR-19b-4 新加)
4. agent 全部完成时输出 `### EXECUTE DONE ###`, Step 4 头部变 **"✓ 已完成"**

### 验证副作用

```bash
# agent 应当实际创建了 docs/hello.md
ls -la docs/hello.md
cat docs/hello.md
# 应该看到: hello from freeform agent
```

### 收尾

- 关 modal: 点 ✕ / Esc / 背景 → **agent 继续在后台跑** (sentinel 协议: 关 modal 不停 agent)
- 真停 agent: Agents 页找到 `agent-team-xxx` 卡片, 点 "终止" → 二次确认 → 调 `DELETE /api/agent/{team_id}`

## 错误路径

### Agent 报错

如果 agent 在任意阶段输出 `### ERROR: <reason> ###`:
- modal 头部下方出现红色 banner: **"Agent 报错: <reason>"** (PR-19b-4 新加)
- agent 进程仍跑, 用户可在 Step 2 继续输入修正问题

### SSE 断连

如果浏览器到后端连接断 (后端重启 / 网络抖):
- pane 区右上角显示 **"connection lost (retrying...)"** (灰色)
- `EventSource` 默认自动重连, 重连成功后 banner 消失
- 不会自动刷页面, agent 继续跑

### Backend 起不来

- Step 1 点 "下一步" 返 500 → toast 红色: `启动 Agent 失败: <detail>`
- 常见原因: tmux 装错 / claude 不在 PATH / dashboard 没权调 subprocess

## 后端协议参考

agent 必须按 `mcp/court-mcp/freeform_bootstrap.txt` 协议输出 sentinel, 否则前端 stuck:

| Sentinel | 触发前端 |
|---|---|
| `### REQ READY ###` | (predicate ready, 暂未用于自动切 step) |
| `### PLAN READY ###` | Step 2 "下一步" 按钮解锁 |
| `### EXECUTE START ###` | Step 3 → Step 4 自动切换 |
| `### TASK DONE: <name> ###` | Step 4 头部进度更新 |
| `### EXECUTE DONE ###` | Step 4 状态切 "已完成" |
| `### ERROR: <reason> ###` | 任意 step 顶部红 banner |

## API 调用清单 (E2E 跑一次会触发的所有 endpoint)

| 时机 | Method + Path |
|---|---|
| Step 1 → 2 | `POST /api/agent/freeform-spawn` |
| Step 2 之后持续 | `GET /api/agent/{team_id}/pane/stream` (SSE) |
| Step 2 textarea 提交 / Step 3 "开工" | `POST /api/agent/{team_id}/input` |
| 终止 agent (Agents 页) | `DELETE /api/agent/{team_id}` |
| Agents 列表刷新 | `GET /api/agent-teams` |

## 时序故障排查

```
症状: modal 卡 Step 2, "下一步" 一直 disabled
原因: agent 没输出 ### PLAN READY ### sentinel
排查: capture-pane 看 agent 是不是卡在 /req 提问没人答 / 或 claude session 崩了
解决: 在 textarea 继续答问题, 或者关 modal + 在 Agents 页终止 agent 重来
```

```
症状: Step 4 头部不显示 "已完成 N 个任务"
原因: agent 没按协议输出 ### TASK DONE: <name> ### (大小写敏感, 严格按格式)
排查: 看 pane 截图里的 sentinel 行格式是否正确 (三个 # + 空格 + TASK DONE: + 空格 + 名字)
```

```
症状: SSE 连不上, 状态栏长期显示 "connection lost"
原因: token 不匹配 / dashboard 进程死了 / 浏览器禁了 EventSource
排查: curl -N -H "Authorization: token <T>" http://127.0.0.1:9100/api/agent/agent-team-x/pane/stream?t=<T>
解决: 如果 curl 也卡, dashboard 进程问题; 否则浏览器问题 (DevTools Network 看 EventSource 状态)
```
