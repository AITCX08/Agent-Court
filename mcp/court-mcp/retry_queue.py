"""SY-4 (#17) retry queue with exponential backoff.

单文件持久化, 单进程读写 (调用方应已经持有 ``seen_state.state_lock`` 之类的
外部锁; 这个模块只用本进程 atomic write + os.replace 防半写, 不做跨进程锁).

设计哲学:
- 失败 → ``push(issue_key, error)`` 进队列, next_at = now + base * (2 ** attempt)
- 超过 ``max_attempts`` → dead letter (从队列移除, 调用方通过返回值知道并自己
  upstream 关 issue / 评论汇报)
- watcher 每 tick 调 ``pop_due(now)`` 拉到点的 issue_key, 加入 dispatch 候选

跟 dashboard_aggregator 集成: caller 可调 ``snapshot()`` 拿全部 RetryItem 列表,
聚合到 ``/api/status`` 让 UI 显示 "retry queue" 面板.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 60


@dataclass(frozen=True)
class RetryItem:
    issue_key: str     # ``<repo>#<num>``
    attempt: int       # 当前是第几次失败 (1 = 首次失败入队)
    next_at: float     # unix ts; 此时之前 pop_due 不会返
    last_error: str
    last_failed_at: float

    def is_due(self, now: float) -> bool:
        return self.next_at <= now


@dataclass(frozen=True)
class DeadLetter:
    issue_key: str
    attempt: int
    last_error: str
    gave_up_at: float


class RetryQueue:
    def __init__(
        self,
        state_dir: Path,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_backoff_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        now: "callable | None" = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if base_backoff_seconds <= 0:
            raise ValueError(f"base_backoff_seconds must be > 0, got {base_backoff_seconds}")
        self._state_dir = state_dir
        self._path = state_dir / "retry-queue.json"
        self._max_attempts = max_attempts
        self._base = float(base_backoff_seconds)
        self._now = now or time.time

    # ------------------------------------------------------------------
    # Persistence layer (atomic write + os.replace)
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _atomic_save(self, data: dict[str, dict]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=self._state_dir,
            prefix=f".{self._path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_name = handle.name
        os.replace(tmp_name, self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, issue_key: str, error: str) -> RetryItem | DeadLetter:
        """记录一次失败. 已超 max_attempts → 返 DeadLetter (从队列移除)."""
        now = self._now()
        data = self._load()
        existing = data.get(issue_key) or {}
        attempt = int(existing.get("attempt", 0)) + 1
        if attempt > self._max_attempts:
            data.pop(issue_key, None)
            self._atomic_save(data)
            return DeadLetter(
                issue_key=issue_key,
                attempt=attempt - 1,  # 之前已 attempt 这么多次都失败了
                last_error=error,
                gave_up_at=now,
            )
        # 指数退避: attempt=1 → base * 2^0 = base; attempt=2 → base * 2; ...
        delay = self._base * (2 ** (attempt - 1))
        item = RetryItem(
            issue_key=issue_key,
            attempt=attempt,
            next_at=now + delay,
            last_error=error,
            last_failed_at=now,
        )
        data[issue_key] = asdict(item)
        self._atomic_save(data)
        return item

    def pop_due(self, now: float | None = None) -> list[str]:
        """到点可以重试的 issue_key 列表. 返回后会从队列移除 (调用方失败再调 push)."""
        t = now if now is not None else self._now()
        data = self._load()
        due: list[str] = []
        for key, raw in list(data.items()):
            try:
                next_at = float(raw.get("next_at", 0))
            except (TypeError, ValueError):
                continue
            if next_at <= t:
                due.append(key)
                data.pop(key)
        if due:
            self._atomic_save(data)
        return due

    def remove(self, issue_key: str) -> bool:
        """成功 dispatch 后调; 清掉队列里的条目. 返 True 表示真的清了."""
        data = self._load()
        if issue_key not in data:
            return False
        data.pop(issue_key)
        self._atomic_save(data)
        return True

    def snapshot(self) -> list[RetryItem]:
        """返当前队列所有 item; UI 面板用."""
        data = self._load()
        out: list[RetryItem] = []
        for key, raw in data.items():
            try:
                out.append(RetryItem(
                    issue_key=key,
                    attempt=int(raw.get("attempt", 0)),
                    next_at=float(raw.get("next_at", 0)),
                    last_error=str(raw.get("last_error", "")),
                    last_failed_at=float(raw.get("last_failed_at", 0)),
                ))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda it: it.next_at)
        return out

    def __len__(self) -> int:
        return len(self._load())
