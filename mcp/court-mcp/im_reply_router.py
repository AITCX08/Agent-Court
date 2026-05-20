"""ImReplyRouter: dashboard 模式下的异步审批结果路由器.

监听 ``pending-approval/*-intake.result``:
- approve → 加载对应 ``pending-intake-context/<slug>-<num>.json`` (watcher 落的 issue+decision 上下文),
  调 ``bin/spawn-issue-window`` 起 Claude window, 更新 seen-issues.json last_action=DISPATCHED_DASHBOARD
- reject → 评论 + close issue, 更新 seen-issues.json last_action=REJECTED_DASHBOARD

PR-13 不再监听 PLAN result (C6): plan 阶段由 ``dual_channel_approval.request_plan`` 内部
``_wait_for_result`` 自己 drain, Claude window 内部阻塞读 verdict, 不需要 router 注入.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import seen_state

# Sentinel: 区分 caller 没传 (auto-load) vs 显式传 None (禁用)
_AUTO_LOAD = object()


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_older_than(ts_iso: str, ref_iso: str, seconds: float) -> bool:
    """ts_iso 比 ref_iso 早 ``seconds`` 秒以上? 解析失败返 False (保守)."""
    from datetime import datetime
    if not ts_iso or not ref_iso:
        return False
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        ref = datetime.fromisoformat(ref_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (ref - ts).total_seconds() > seconds


def _split_issue_key(key: str) -> tuple[str, int]:
    """``foo/bar#12`` → (``foo/bar``, 12). 解析失败返 (key, 0)."""
    repo, sep, num_str = key.partition("#")
    if not sep:
        return key, 0
    try:
        return repo, int(num_str)
    except ValueError:
        return repo, 0


class ImReplyRouter:
    def __init__(
        self,
        court_root: Path,
        *,
        poll_interval: float = 1.0,
        gitea_client=None,
        spawn_window_bin: Path | None = None,
        workflow_config: "Any" = _AUTO_LOAD,
        retry_queue: "Any" = _AUTO_LOAD,
        active_court_counter: "callable | None" = None,
    ) -> None:
        self.court_root = court_root
        self.poll_interval = poll_interval
        self.pending_dir = self.court_root / "gitea-watcher" / "pending-approval"
        self.ctx_dir = self.court_root / "gitea-watcher" / "pending-intake-context"
        self.processed_dir = self.pending_dir / ".processed"
        self._seen_results: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gitea_client = gitea_client
        # 默认 bin 路径: <repo_root>/bin/spawn-issue-window
        self.spawn_window_bin = spawn_window_bin or (Path(__file__).resolve().parents[2] / "bin" / "spawn-issue-window")
        # SY-4 #17: bounded concurrency + retry/backoff
        # workflow_config / retry_queue 用 _AUTO_LOAD sentinel 区分:
        #   - caller 不传 → 自动 load (生产路径)
        #   - caller 显式传 None → 禁用 (测试 / 灾难逃生)
        #   - caller 传具体值 → 用 caller 的 (测试注入 / DI)
        if workflow_config is _AUTO_LOAD:
            self.workflow_config = self._maybe_load_workflow_config()
        else:
            self.workflow_config = workflow_config
        if retry_queue is _AUTO_LOAD:
            self.retry_queue = self._build_retry_queue()
        else:
            self.retry_queue = retry_queue
        # Override-able 钩子: 让测试可注入假 counter
        self._active_court_counter = active_court_counter or self._count_active_courts_via_tmux

    @staticmethod
    def _maybe_load_workflow_config() -> "Any | None":
        try:
            from workflow_loader import load_workflow
            repo_root = Path(__file__).resolve().parents[2]
            return load_workflow(repo_root).config
        except Exception:
            return None

    def _build_retry_queue(self):
        try:
            from retry_queue import (
                DEFAULT_BACKOFF_BASE_SECONDS,
                DEFAULT_MAX_ATTEMPTS,
                RetryQueue,
            )
        except ImportError:
            return None
        cfg = self.workflow_config
        return RetryQueue(
            state_dir=self.court_root / "gitea-watcher",
            max_attempts=getattr(cfg, "retry_max", DEFAULT_MAX_ATTEMPTS) if cfg else DEFAULT_MAX_ATTEMPTS,
            base_backoff_seconds=getattr(cfg, "retry_backoff_base_seconds", DEFAULT_BACKOFF_BASE_SECONDS) if cfg else DEFAULT_BACKOFF_BASE_SECONDS,
        )

    def _count_active_courts_via_tmux(self) -> int:
        """数 dashboard session 里非 watcher 的 window. 失败返 0 (保守不阻 dispatch)."""
        try:
            from dashboard_tmux import SESSION_NAME, WATCHER_WINDOW
            result = subprocess.run(
                ["tmux", "list-windows", "-t", SESSION_NAME, "-F", "#{window_name}"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return 0
            names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
            return sum(1 for n in names if n != WATCHER_WINDOW)
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            return 0

    def _at_capacity(self) -> bool:
        cfg = self.workflow_config
        if cfg is None:
            return False  # 没配置时不限制, 老行为
        cap = getattr(cfg, "max_concurrent_runs", 0)
        if cap <= 0:
            return False
        return self._active_court_counter() >= cap

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="im-reply-router", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:  # pragma: no cover - defensive: 线程不能死
                print(f"[router] scan failed: {exc!r}", file=sys.stderr, flush=True)
            time.sleep(self.poll_interval)

    def scan_once(self) -> int:
        """单步扫描, 返回处理的 result 数. 测试用."""
        return self._scan_once()

    def _scan_once(self) -> int:
        if not self.pending_dir.is_dir():
            return 0
        count = 0
        for result_path in sorted(self.pending_dir.glob("*-intake.result")):
            if result_path.name in self._seen_results:
                continue
            try:
                self._handle_intake_result(result_path)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[router] handle {result_path.name} failed: {exc!r}", file=sys.stderr, flush=True)
            self._seen_results.add(result_path.name)
            count += 1
        # SY-4 review R-1: 也消费 retry queue 到点条目; 没人 tick → defer 的 issue 永远卡
        count += self._retry_due_items()
        # SY-4 review Mi-2: 也扫超时的 court window
        count += self._enforce_run_timeout()
        return count

    def _retry_due_items(self) -> int:
        """SY-4 review R-1: 把 retry queue 里到点的 issue 拿回来重新 dispatch.

        从 intake_context 找回 issue+decision+comments, 从 seen-issues 取历史 winner,
        重新走 _dispatch_approved (含 capacity check + 再失败再 push).
        """
        if self.retry_queue is None:
            return 0
        try:
            due_keys = self.retry_queue.pop_due()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[router] retry pop_due failed: {exc!r}", file=sys.stderr, flush=True)
            return 0
        n = 0
        for key in due_keys:
            try:
                repo, sep, num_str = key.partition("#")
                if not sep:
                    continue
                num = int(num_str)
            except ValueError:
                continue
            ctx = self._load_intake_context(repo, num)
            if ctx is None:
                # context 丢了, 没法恢复; 不重新 push (会无限循环)
                print(f"[router] retry skip {key}: missing intake context", file=sys.stderr, flush=True)
                continue
            try:
                seen = seen_state.load_seen(self.court_root / "gitea-watcher")
            except OSError:
                seen = {}
            entry = seen.get(key, {}) if isinstance(seen, dict) else {}
            winner = entry.get("approval_winner", "retry")
            try:
                self._dispatch_approved(repo, num, ctx, winner)
                n += 1
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[router] retry dispatch {key} failed: {exc!r}", file=sys.stderr, flush=True)
        return n

    def _enforce_run_timeout(self) -> int:
        """SY-4 review Mi-2: 跑超时的 court window 强制 kill + 进 retry queue.

        timeout 来源 WORKFLOW.md run_timeout_seconds (cfg 缺失则不 enforce).
        判定: seen-issues entry 有 dispatched_at 且距今 > timeout → kill window.
        """
        cfg = self.workflow_config
        if cfg is None:
            return 0
        timeout_s = getattr(cfg, "run_timeout_seconds", 0)
        if not timeout_s or timeout_s <= 0:
            return 0
        try:
            seen = seen_state.load_seen(self.court_root / "gitea-watcher")
        except OSError:
            return 0
        if not isinstance(seen, dict):
            return 0
        now_iso = _iso_now()
        n = 0
        for key, entry in list(seen.items()):
            if not isinstance(entry, dict):
                continue
            if entry.get("last_action") != "DISPATCHED_DASHBOARD":
                continue
            dispatched_at = entry.get("dispatched_at", "")
            if not _iso_older_than(dispatched_at, now_iso, timeout_s):
                continue
            window_name = entry.get("tmux_window") or ""
            if not window_name:
                continue
            try:
                self._kill_tmux_window(window_name)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[router] timeout kill {key} ({window_name}) failed: {exc!r}", file=sys.stderr, flush=True)
                continue
            if self.retry_queue is not None:
                self.retry_queue.push(key, f"timeout after {timeout_s}s")
            seen_state.update_entry(
                _split_issue_key(key)[0], _split_issue_key(key)[1],
                {
                    "last_action": "TIMEOUT_KILLED",
                    "timeout_killed_at": now_iso,
                },
            )
            print(f"[router] timeout kill {key} ({window_name}, after {timeout_s}s)", file=sys.stderr, flush=True)
            n += 1
        return n

    def _kill_tmux_window(self, window_name: str) -> None:
        """安全 kill: 校验 window 名只含 path-safe 字符."""
        if not window_name or "/" in window_name or any(c in window_name for c in (" ", ";", "&", "|", "$", "`")):
            raise ValueError(f"unsafe window name: {window_name!r}")
        from dashboard_tmux import SESSION_NAME
        subprocess.run(
            ["tmux", "kill-window", "-t", f"{SESSION_NAME}:{window_name}"],
            capture_output=True,
            timeout=3,
            check=False,
        )

    def _handle_intake_result(self, result_path: Path) -> None:
        try:
            meta = json.loads(result_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"[router] {result_path.name} 不是合法 JSON: {exc}; 已跳过", file=sys.stderr, flush=True)
            self._archive(result_path, reason="invalid-json")
            return

        repo = meta.get("repo", "")
        num = int(meta.get("number", 0))
        verdict = meta.get("verdict", "")
        winner = meta.get("winner", "?")

        ctx = self._load_intake_context(repo, num)
        if ctx is None:
            print(f"[router] missing intake context for {repo}#{num}; 已跳过", file=sys.stderr, flush=True)
            self._archive(result_path, reason="missing-context")
            return

        if verdict == "approve":
            self._dispatch_approved(repo, num, ctx, winner)
        elif verdict == "reject":
            self._dispatch_rejected(repo, num, meta.get("reason", ""), winner)
        else:
            print(f"[router] unsupported verdict {verdict!r} for {repo}#{num}", file=sys.stderr, flush=True)

        self._archive(result_path, reason=verdict or "unknown")

    def _dispatch_approved(self, repo: str, num: int, ctx: dict[str, Any], winner: str) -> None:
        issue = ctx["issue"]
        decision = ctx["decision"]
        comments = ctx.get("comments", [])
        issue_key = f"{repo}#{num}"

        # SY-4 #17: 容量上限. 超 max_concurrent_runs → 进 retry queue 等下个 tick
        if self._at_capacity():
            if self.retry_queue is not None:
                self.retry_queue.push(issue_key, "deferred: at concurrency cap")
            seen_state.update_entry(repo, num, {
                "last_action": "DEFERRED_CAPACITY",
                "approval_winner": winner,
                "stage": "INTAKE",
                "deferred_at": _iso_now(),
            })
            print(f"[router] deferred {issue_key}: at concurrency cap", file=sys.stderr, flush=True)
            return

        # 写 intro 给 spawn-issue-window 加载
        from issue_resolver import build_intro_message
        intro = build_intro_message(issue, comments, decision)
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as handle:
            handle.write(intro)
            intro_path = handle.name

        try:
            subprocess.run(
                [str(self.spawn_window_bin), repo, str(num), intro_path],
                check=True,
                env=self._safe_env(),
            )
        except subprocess.CalledProcessError as exc:
            print(f"[router] spawn-issue-window failed for {repo}#{num}: {exc}", file=sys.stderr, flush=True)
            # SY-4: 失败 → retry queue. 超 max_attempts 时 push 返 DeadLetter
            if self.retry_queue is not None:
                self.retry_queue.push(issue_key, f"spawn-issue-window failed: {exc}")
            seen_state.update_entry(repo, num, {
                "last_action": "SPAWN_FAILED",
                "approval_winner": winner,
                "stage": "INTAKE",
                "spawn_error": str(exc),
            })
            return

        # SY-4: dispatch 成功 → 清掉 retry queue 里之前失败的条目
        if self.retry_queue is not None:
            self.retry_queue.remove(issue_key)

        from dashboard_tmux import issue_window_name
        window_name = issue_window_name(repo, num)
        seen_state.update_entry(repo, num, {
            "last_action": "DISPATCHED_DASHBOARD",
            "approval_winner": winner,
            "tmux_window": window_name,
            "dispatched_at": _iso_now(),
            "stage": "INTAKE",
        })

    def _dispatch_rejected(self, repo: str, num: int, reason: str, winner: str) -> None:
        client = self._client()
        try:
            comment_body = reason.strip() or f"intake 审批未通过 (by {winner})"
            client.comment_on_issue(repo, num, comment_body)
            client.transition_issue(repo, num, "closed")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[router] reject api call failed for {repo}#{num}: {exc!r}", file=sys.stderr, flush=True)
        seen_state.update_entry(repo, num, {
            "last_action": "REJECTED_DASHBOARD",
            "approval_winner": winner,
            "stage": "INTAKE",
        })

    def _load_intake_context(self, repo: str, num: int) -> dict[str, Any] | None:
        slug = repo.replace("/", "-").lower()
        ctx_path = self.ctx_dir / f"{slug}-{num}.json"
        if not ctx_path.is_file():
            return None
        try:
            return json.loads(ctx_path.read_text())
        except json.JSONDecodeError:
            return None

    def _archive(self, result_path: Path, *, reason: str) -> None:
        """把处理过的 .result 移到 .processed/, 避免下一轮重复 scan."""
        try:
            self.processed_dir.mkdir(parents=True, exist_ok=True)
            target = self.processed_dir / f"{result_path.stem}.{reason}.json"
            result_path.rename(target)
        except OSError:
            try:
                result_path.unlink()
            except OSError:
                pass

    def _client(self):
        if self._gitea_client is None:
            from gitea_client import GiteaClient
            self._gitea_client = GiteaClient()
        return self._gitea_client

    @staticmethod
    def _safe_env() -> dict[str, str]:
        keys = {"PATH", "HOME", "USER", "SHELL", "TERM", "TMPDIR", "COURT_ROOT", "LANG", "LC_ALL"}
        return {k: v for k, v in os.environ.items() if k in keys}
