#!/usr/bin/env bash
# PR-14 e2e: 模拟 Gitea webhook POST 到本地 receiver, 验证全链路:
# 1. 起 receiver 前台 (后台进程)
# 2. openssl 计算 HMAC-SHA256 签名
# 3. curl POST issue 事件 (issues:assigned)
# 4. 验证 pending-webhook/*.json 落盘
# 5. watcher run-once 消费 webhook, 验证 seen-issues 含 source=webhook
# 6. .processed/ 归档
#
# 不依赖真 git.k2lab.ai. 用 stub SHENLI_COMMAND + StubClient 替换.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY_DIR="$ROOT/mcp/court-mcp"
PYTHON_BIN="$PY_DIR/.venv/bin/python3"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "[e2e] missing venv python: $PYTHON_BIN" >&2
  exit 0
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agent-court-webhook-e2e.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"; [ -n "${RECEIVER_PID:-}" ] && kill "$RECEIVER_PID" 2>/dev/null || true' EXIT

# 选个空闲端口 (lsof 检测; 简化用固定 18765)
PORT=18765
SECRET="e2e-test-secret-pr14"

# 1. 起 receiver (cd 到 PY_DIR 才能 import gitea_webhook_receiver)
# 用 cd + env, 不用 subshell, 否则 $! 拿到的是 subshell PID, trap 杀不到 receiver
mkdir -p "$TMP_ROOT/gitea-watcher"
cd "$PY_DIR"
env \
  COURT_ROOT="$TMP_ROOT" \
  WEBHOOK_PORT="$PORT" \
  WEBHOOK_BIND="127.0.0.1" \
  K2LAB_WEBHOOK_SECRET="$SECRET" \
  WEBHOOK_ASSIGNEE="wjx" \
  "$PYTHON_BIN" -m gitea_webhook_receiver --port "$PORT" --bind 127.0.0.1 &
RECEIVER_PID=$!
cd - >/dev/null

# 等 receiver 起来
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.3
done
if ! curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null; then
  echo "[e2e] receiver 启动失败" >&2
  exit 1
fi
echo "[e2e] receiver ready on :$PORT"

# 2. 构造 payload + HMAC 签名
PAYLOAD='{"action":"assigned","issue":{"number":42,"title":"e2e fixture","html_url":"http://git.k2lab.ai/K2Lab/e2e/issues/42","body":"e2e","assignees":[{"login":"wjx"}],"labels":[],"updated_at":"2026-05-19T20:00:00Z"},"repository":{"full_name":"K2Lab/e2e"},"sender":{"login":"tester"}}'
SIGNATURE="$(printf '%s' "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')"

# 3. POST
HTTP_CODE="$(curl -s -o "$TMP_ROOT/resp.json" -w "%{http_code}" \
  -X POST "http://127.0.0.1:$PORT/gitea/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Gitea-Event: issues" \
  -H "X-Gitea-Delivery: e2e-uuid-1" \
  -H "X-Gitea-Signature: $SIGNATURE" \
  -d "$PAYLOAD")"

if [ "$HTTP_CODE" != "200" ]; then
  echo "[e2e] POST 失败 status=$HTTP_CODE body=$(cat "$TMP_ROOT/resp.json")" >&2
  exit 1
fi
echo "[e2e] webhook accepted: $(cat "$TMP_ROOT/resp.json")"

# 4. 验证 pending-webhook/*.json 落盘
PENDING_DIR="$TMP_ROOT/gitea-watcher/pending-webhook"
FILES="$(ls "$PENDING_DIR"/*.json 2>/dev/null || true)"
if [ -z "$FILES" ]; then
  echo "[e2e] FAIL: pending-webhook 没落盘" >&2
  exit 1
fi
echo "[e2e] pending-webhook 落盘: $(basename "$FILES")"

# 5. watcher run-once 消费 (用 stub SHENLI_COMMAND 不调真 LLM)
STUB_DIR="$(mktemp -d "$TMP_ROOT/shenli-stub.XXXXXX")"
cat > "$STUB_DIR/shenli_stub.py" <<'STUB_EOF'
import json
print(json.dumps({"decision": "NEED_INFO", "comment_body": "e2e auto"}))
STUB_EOF

# 用 mock GiteaClient (stub list_assigned_issues 返空, 让 watcher 100% 走 webhook 链路)
cat > "$STUB_DIR/run_watcher.py" <<'WATCHER_EOF'
import os, sys, json
sys.path.insert(0, os.environ["PY_DIR"])
from gitea_watcher import GiteaWatcher

class StubClient:
    def list_assigned_issues(self, state="open", since=None): return []
    def get_issue(self, repo, num):
        return {"number": num, "title": f"detail-{num}", "repository": {"full_name": repo}, "updated_at": "2026-05-19T20:00:00Z"}
    def list_issue_comments(self, repo, num): return []
    def comment_on_issue(self, repo, num, body):
        print(f"[stub] comment {repo}#{num}: {body[:50]}")
        return {}
    def transition_issue(self, repo, num, state): return {}

state_dir = os.environ["COURT_ROOT"] + "/gitea-watcher"
# seed seen 避免 bootstrap 跳过
import pathlib
(pathlib.Path(state_dir) / "seen-issues.json").write_text(json.dumps({
    "K2Lab/dummy#999": {"repo": "K2Lab/dummy", "number": 999, "last_action": "BOOTSTRAP", "updated_at": "old"}
}))
w = GiteaWatcher(court_root=pathlib.Path(os.environ["COURT_ROOT"]), client=StubClient(), mode="court")
result = w.run_once()
print(json.dumps(result))
WATCHER_EOF

COURT_ROOT="$TMP_ROOT" \
PY_DIR="$PY_DIR" \
SHENLI_COMMAND="$PYTHON_BIN $STUB_DIR/shenli_stub.py" \
"$PYTHON_BIN" "$STUB_DIR/run_watcher.py"

# 6. 验证 seen-issues 包含 source=webhook
SEEN="$TMP_ROOT/gitea-watcher/seen-issues.json"
ENTRY="$("$PYTHON_BIN" -c "import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(d.get('K2Lab/e2e#42', {})))" "$SEEN")"
if ! echo "$ENTRY" | grep -q '"source": "webhook"'; then
  echo "[e2e] FAIL: K2Lab/e2e#42 没标 source=webhook; entry=$ENTRY" >&2
  exit 1
fi
if ! echo "$ENTRY" | grep -q '"webhook_event_id": "e2e-uuid-1"'; then
  echo "[e2e] FAIL: webhook_event_id 缺失; entry=$ENTRY" >&2
  exit 1
fi

# 验证归档
PROCESSED="$(ls "$PENDING_DIR/.processed"/*.json 2>/dev/null || true)"
if [ -z "$PROCESSED" ]; then
  echo "[e2e] FAIL: pending-webhook 没归档到 .processed/" >&2
  exit 1
fi

echo "[e2e] PR-14 webhook full path OK"
