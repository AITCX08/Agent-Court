"""SY-3 (#18) Orchestrator — 4 处状态收拢的统一视图层 (MVP v1).

agent-court 的状态目前散在 4 处:

| 状态 | 文件 |
|---|---|
| 已见 issue + last_action | ``seen-issues.json`` (PR-13) |
| 待审批 INTAKE/PLAN | ``pending-approval/<slug>.json`` (+ .lock / .result) (PR-13) |
| webhook 队列 | ``pending-webhook/<delivery>.json`` (PR-14) |
| 跑着的 court window | tmux ``agent-court-dashboard`` session windows (PR-13) |

加 SY-4 后多一处:
- ``retry-queue.json`` (重试)

bug 类型 (来自 issue #18 的诊断):
- pending 已 approve 但 tmux window 没起来 → 状态机断了
- tmux window 残留 (claude crash 没退干净) → seen 标 EXECUTING, 新轮询不再 dispatch
- webhook + polling 双源同时进 → 偶发重复 spawn

## MVP v1 范围 (本模块)

- **不重写写路径**: watcher / router / approval / resolver 现状不动
- 提供**单一只读统一视图** ``snapshot()``: 把 4+1 处状态 join 成 ``list[Run]``
- ``reconcile()``: 检测 4 类不一致, 返 ``list[Inconsistency]`` (不主动修, 让 caller
  或 dashboard UI 决定)
- ``get_metrics()`` 统计供监控用

v2 (留下一 PR): 把写路径也收拢到 ``Orchestrator``, watcher/router 改为只投事件,
状态唯一源是内存 + 单一持久化文件 ``runs.json``. 那时本模块的 4 个文件读入逻辑
退化成 hydrate-on-startup helper.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


SESSION_NAME = "agent-court-dashboard"
WATCHER_WINDOW = "watcher"


class RunState(str, Enum):
    """跟 seen_state.last_action 一一对应 + 几个派生状态."""
    QUEUED = "queued"                  # 在 retry queue / pending-webhook 等
    BOOTSTRAP = "bootstrap"            # 首次启动 hydrate, 历史 issue
    PENDING_APPROVAL = "pending_approval"  # pending-approval/*.json 在 (INTAKE/PLAN)
    DISPATCHED = "dispatched"          # last_action=DISPATCHED_DASHBOARD, tmux window 应该在
    EXECUTING = "executing"            # last_action=EXECUTING (claude 在跑)
    DONE = "done"                       # last_action=DONE_DASHBOARD
    FAILED = "failed"                   # last_action=SPAWN_FAILED
    REJECTED = "rejected"               # last_action=REJECTED_DASHBOARD
    DEFERRED_CAPACITY = "deferred_capacity"
    TIMEOUT_KILLED = "timeout_killed"
    UNKNOWN = "unknown"


_LAST_ACTION_TO_STATE: dict[str, RunState] = {
    "BOOTSTRAP": RunState.BOOTSTRAP,
    "DISPATCHED_DASHBOARD": RunState.DISPATCHED,
    "EXECUTING": RunState.EXECUTING,
    "DONE_DASHBOARD": RunState.DONE,
    "SPAWN_FAILED": RunState.FAILED,
    "REJECTED_DASHBOARD": RunState.REJECTED,
    "DEFERRED_CAPACITY": RunState.DEFERRED_CAPACITY,
    "TIMEOUT_KILLED": RunState.TIMEOUT_KILLED,
}


@dataclass(frozen=True)
class Run:
    """单个 issue 的统一视图 (4+1 处状态 join 后的派生)."""
    issue_key: str       # "<repo>#<num>"
    repo: str
    number: int
    state: RunState
    stage: str = ""               # INTAKE | PLAN | ""
    last_action: str = ""         # 原 seen.last_action (向后兼容字符串)
    winner: str = ""              # approval_winner
    tmux_window: str = ""         # 名字; "" = 没 window
    tmux_window_alive: bool = False
    has_pending_approval: bool = False
    in_retry_queue: bool = False
    retry_attempt: int = 0
    dispatched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"


@dataclass(frozen=True)
class Inconsistency:
    """``reconcile()`` 找到的不一致. caller 决定要不要自动修."""
    issue_key: str
    kind: str
    severity: str
    detail: str
    suggested_fix: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Snapshot:
    runs: list[Run]
    inconsistencies: list[Inconsistency]
    metrics: dict[str, int]
    orphan_tmux_windows: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": [r.to_dict() for r in self.runs],
            "inconsistencies": [i.to_dict() for i in self.inconsistencies],
            "metrics": self.metrics,
            "orphan_tmux_windows": list(self.orphan_tmux_windows),
        }


class Orchestrator:
    def __init__(
        self,
        court_root: Path | None = None,
        *,
        session_name: str = SESSION_NAME,
        watcher_window: str = WATCHER_WINDOW,
    ) -> None:
        self.court_root = court_root or (Path.home() / ".agent-court")
        self.state_dir = self.court_root / "gitea-watcher"
        self.session_name = session_name
        self.watcher_window = watcher_window

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        seen = self._load_seen()
        pending_keys = self._collect_pending_approval_keys()
        retry_map = self._collect_retry_queue()
        tmux_windows = self._collect_tmux_windows()
        tmux_window_set = set(tmux_windows)

        runs: list[Run] = []
        seen_window_names: set[str] = set()
        for key, raw in seen.items():
            run = _build_run_from_seen(
                issue_key=key,
                raw=raw,
                has_pending_approval=key in pending_keys,
                in_retry_queue=key in retry_map,
                retry_attempt=retry_map.get(key, 0),
                tmux_windows=tmux_window_set,
            )
            if run is not None:
                runs.append(run)
                if run.tmux_window:
                    seen_window_names.add(run.tmux_window)
        # 把只在 retry queue / pending 里, 但 seen 还没记的 issue 也补成 Run
        for key in (set(pending_keys) | set(retry_map)) - set(seen.keys()):
            repo, num = _split_key(key)
            if not repo or num is None:
                continue
            runs.append(Run(
                issue_key=key,
                repo=repo,
                number=num,
                state=RunState.QUEUED,
                has_pending_approval=(key in pending_keys),
                in_retry_queue=(key in retry_map),
                retry_attempt=retry_map.get(key, 0),
                stage="INTAKE" if key in pending_keys else "",
            ))
        orphan_windows = [
            w for w in tmux_windows
            if w != self.watcher_window and w not in seen_window_names
        ]
        inconsistencies = self._reconcile_internal(
            runs=runs,
            seen=seen,
            tmux_windows=tmux_window_set,
            orphan_windows=orphan_windows,
        )
        metrics = self._compute_metrics(runs, inconsistencies, orphan_windows)
        return Snapshot(
            runs=runs,
            inconsistencies=inconsistencies,
            metrics=metrics,
            orphan_tmux_windows=orphan_windows,
        )

    def reconcile(self) -> list[Inconsistency]:
        """返当前 4+1 处状态间的不一致清单. 不主动修."""
        return self.snapshot().inconsistencies

    def get_run(self, issue_key: str) -> Run | None:
        for r in self.snapshot().runs:
            if r.issue_key == issue_key:
                return r
        return None

    def get_metrics(self) -> dict[str, int]:
        return self.snapshot().metrics

    # ------------------------------------------------------------------
    # 状态收集 (4+1 处, 全部容错)
    # ------------------------------------------------------------------

    def _load_seen(self) -> dict[str, Any]:
        path = self.state_dir / "seen-issues.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _collect_pending_approval_keys(self) -> set[str]:
        """pending-approval/<slug>.json 还在 + .result 还没出现 → 进 set."""
        pending_dir = self.state_dir / "pending-approval"
        if not pending_dir.is_dir():
            return set()
        keys: set[str] = set()
        for path in pending_dir.glob("*.json"):
            if not path.is_file():
                continue
            slug = path.stem
            if (pending_dir / f"{slug}.result").exists():
                continue
            try:
                meta = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            repo = meta.get("repo")
            num = meta.get("number")
            if isinstance(repo, str) and isinstance(num, int):
                keys.add(f"{repo}#{num}")
        return keys

    def _collect_retry_queue(self) -> dict[str, int]:
        """issue_key → 当前 attempt 次数. SY-4 retry_queue 文件."""
        path = self.state_dir / "retry-queue.json"
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, int] = {}
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                out[key] = int(entry.get("attempt", 0))
            except (TypeError, ValueError):
                out[key] = 0
        return out

    def _collect_tmux_windows(self) -> list[str]:
        """``tmux list-windows -t <session> -F '#{window_name}'`` → list. tmux 不可用返 []."""
        try:
            proc = subprocess.run(
                ["tmux", "list-windows", "-t", self.session_name, "-F", "#{window_name}"],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]

    # ------------------------------------------------------------------
    # Reconcile (不一致检测)
    # ------------------------------------------------------------------

    def _reconcile_internal(
        self,
        *,
        runs: list[Run],
        seen: dict[str, Any],
        tmux_windows: set[str],
        orphan_windows: list[str],
    ) -> list[Inconsistency]:
        out: list[Inconsistency] = []
        # I-1: seen=DISPATCHED 但 tmux window 不在 → claude 已 crash, 状态没更新
        for r in runs:
            if r.state == RunState.DISPATCHED and r.tmux_window and not r.tmux_window_alive:
                out.append(Inconsistency(
                    issue_key=r.issue_key,
                    kind="dispatched_window_gone",
                    severity=SEVERITY_ERROR,
                    detail=f"seen.last_action=DISPATCHED_DASHBOARD 但 tmux window {r.tmux_window!r} 已不存在",
                    suggested_fix="orchestrator 标 FAILED + push retry queue, 或 caller 手动 spawn",
                ))
            # I-2: seen=EXECUTING 但 tmux window 不在 → claude crash 在中途
            elif r.state == RunState.EXECUTING and r.tmux_window and not r.tmux_window_alive:
                out.append(Inconsistency(
                    issue_key=r.issue_key,
                    kind="executing_window_gone",
                    severity=SEVERITY_ERROR,
                    detail=f"seen.last_action=EXECUTING 但 tmux window {r.tmux_window!r} 已不存在",
                    suggested_fix="标 FAILED + push retry queue",
                ))
            # I-3: seen 已 DONE 但 retry queue 里还有这条 → stale entry
            if r.state in {RunState.DONE, RunState.REJECTED} and r.in_retry_queue:
                out.append(Inconsistency(
                    issue_key=r.issue_key,
                    kind="retry_stale_after_done",
                    severity=SEVERITY_WARN,
                    detail=f"seen 已 {r.state.value} 但 retry queue 里仍有条目 (attempt={r.retry_attempt})",
                    suggested_fix="orchestrator 调 retry_queue.remove(issue_key)",
                ))
            # I-4: pending-approval 还在但 seen 已 DONE → result 写后没清 pending
            if r.state in {RunState.DONE, RunState.REJECTED} and r.has_pending_approval:
                out.append(Inconsistency(
                    issue_key=r.issue_key,
                    kind="pending_after_done",
                    severity=SEVERITY_WARN,
                    detail=f"seen 已 {r.state.value} 但 pending-approval/*.json 还在",
                    suggested_fix="清掉 pending-approval/<slug>.json + .result + .lock",
                ))
        # I-5: tmux window 在但 seen 完全没记录 (没经过 router 走 spawn) → 手动起的孤儿
        for win in orphan_windows:
            out.append(Inconsistency(
                issue_key="",  # 没对应 issue
                kind="tmux_window_orphan",
                severity=SEVERITY_WARN,
                detail=f"tmux window {win!r} 没对应 seen-issues entry (手动起的? router 漏更新?)",
                suggested_fix="如果是手动测试 window, 可 tmux kill-window; 否则补 seen entry",
            ))
        return out

    def _compute_metrics(
        self,
        runs: list[Run],
        inconsistencies: list[Inconsistency],
        orphan_windows: list[str],
    ) -> dict[str, int]:
        out = {state.value: 0 for state in RunState}
        for r in runs:
            out[r.state.value] += 1
        out["total"] = len(runs)
        out["active"] = sum(1 for r in runs if r.state in {RunState.DISPATCHED, RunState.EXECUTING})
        out["pending_approval_count"] = sum(1 for r in runs if r.has_pending_approval)
        out["in_retry_queue"] = sum(1 for r in runs if r.in_retry_queue)
        out["orphan_tmux_windows"] = len(orphan_windows)
        out["inconsistencies"] = len(inconsistencies)
        out["inconsistencies_error"] = sum(1 for i in inconsistencies if i.severity == SEVERITY_ERROR)
        out["inconsistencies_warn"] = sum(1 for i in inconsistencies if i.severity == SEVERITY_WARN)
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _split_key(key: str) -> tuple[str | None, int | None]:
    repo, sep, num_str = key.partition("#")
    if not sep:
        return None, None
    try:
        return repo, int(num_str)
    except ValueError:
        return repo, None


def _build_run_from_seen(
    *,
    issue_key: str,
    raw: Any,
    has_pending_approval: bool,
    in_retry_queue: bool,
    retry_attempt: int,
    tmux_windows: set[str],
) -> Run | None:
    if not isinstance(raw, dict):
        return None
    repo, num = _split_key(issue_key)
    if not repo or num is None:
        return None
    last_action = str(raw.get("last_action", ""))
    tmux_window = str(raw.get("tmux_window", ""))
    state = _LAST_ACTION_TO_STATE.get(last_action, RunState.UNKNOWN)
    # has_pending_approval 优先级最高: 没 spawn 但 approval 已就绪 → pending_approval
    if has_pending_approval and state in {RunState.UNKNOWN, RunState.BOOTSTRAP}:
        state = RunState.PENDING_APPROVAL
    return Run(
        issue_key=issue_key,
        repo=repo,
        number=num,
        state=state,
        stage=str(raw.get("stage", "")),
        last_action=last_action,
        winner=str(raw.get("approval_winner", "")),
        tmux_window=tmux_window,
        tmux_window_alive=(bool(tmux_window) and tmux_window in tmux_windows),
        has_pending_approval=has_pending_approval,
        in_retry_queue=in_retry_queue,
        retry_attempt=retry_attempt,
        dispatched_at=str(raw.get("dispatched_at", "")),
    )
