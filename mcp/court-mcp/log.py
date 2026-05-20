"""SY-5 (#15) 结构化 JSON 日志.

把 daemon 类模块 (watcher / receiver / dashboard / approval / im_router / yiguan)
原本的 ``print("[xx] msg", file=sys.stderr, flush=True)`` 换成单条 JSON line,
方便 ``jq`` parse + ``grep '"num":12'`` 拉单 issue 全生命周期.

CLI 工具 (lingpai_cli / shenpi_cli / onboard ...) 的 stderr 输出是 human-friendly
操作反馈, 不走这个 logger.

用法::

    from log import get_logger
    log = get_logger("watcher")
    log.info(event="issue_new", repo="foo/bar", num=12, source="webhook")
    log.warning(event="webhook_signature_mismatch", delivery="abc")
    log.error(event="dispatch_failed", repo="foo/bar", num=12, error=str(exc))

输出 (JSON line)::

    {"ts":"2026-05-20T07:45:00Z","level":"info","component":"watcher",
     "event":"issue_new","repo":"foo/bar","num":12,"source":"webhook"}

可选 file sink (``COURT_LOG_FILE`` 环境变量) 让 dashboard UI 可以 tail 文件
做日志面板.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

DEFAULT_LEVEL = logging.INFO
ENV_LOG_FILE = "COURT_LOG_FILE"
ENV_LOG_LEVEL = "COURT_LOG_LEVEL"

# logging.LogRecord 自带这些字段, 不要混入 JSON 输出.
_LOGRECORD_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "component", "event", "message", "taskName",
})


class JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON, 自定义 ``extra={}`` 字段全部并到顶层."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        out: dict[str, Any] = {
            "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "component": getattr(record, "component", "unknown"),
        }
        event = getattr(record, "event", None)
        if event:
            out["event"] = event
        # extra={} 字段全部抬到顶层
        for k, v in record.__dict__.items():
            if k in _LOGRECORD_RESERVED:
                continue
            out[k] = v
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


_initialized = False


def _ensure_init() -> None:
    """幂等初始化 root court logger. 多次 import 不会叠加 handler."""
    global _initialized
    if _initialized:
        return
    root = logging.getLogger("court")
    level_name = os.environ.get(ENV_LOG_LEVEL, "INFO").upper()
    root.setLevel(getattr(logging, level_name, DEFAULT_LEVEL))
    root.propagate = False
    formatter = JsonFormatter()
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)
    log_file = os.environ.get(ENV_LOG_FILE)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            # 文件 sink 失败不影响 stderr; 仅打印一条 fallback warn
            sys.stderr.write(
                json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "level": "warning",
                    "component": "log",
                    "event": "file_sink_init_failed",
                    "path": log_file,
                    "error": str(exc),
                }) + "\n"
            )
    _initialized = True


def reset_for_testing() -> None:
    """测试用: 拆掉所有 handler + 重置 ``_initialized``. 不在生产代码里调."""
    global _initialized
    root = logging.getLogger("court")
    for h in list(root.handlers):
        root.removeHandler(h)
    _initialized = False


class ComponentLogger:
    """轻封装 stdlib logger, 只接 ``event=`` + ``**kv`` 风格.

    raw text 日志统一改写成 event + 描述性 kv (例如:
    ``log.info(event="watcher_start", port=9100)``). 没有 free-form 的
    message named 参数, 避免 caller 不小心把业务字段写成 ``message=`` 后被
    吃掉.
    """

    __slots__ = ("_component", "_logger")

    def __init__(self, component: str, logger: logging.Logger) -> None:
        self._component = component
        self._logger = logger

    def _emit(self, level: int, event: str | None, **kv: Any) -> None:
        extra: dict[str, Any] = {"component": self._component}
        if event:
            extra["event"] = event
        # caller 用了跟 LogRecord 撞名的 key 自动加 kv_ 前缀
        for k, v in kv.items():
            if k in _LOGRECORD_RESERVED:
                extra[f"kv_{k}"] = v
            else:
                extra[k] = v
        self._logger.log(level, "", extra=extra)

    def info(self, event: str | None = None, **kv: Any) -> None:
        self._emit(logging.INFO, event, **kv)

    def warning(self, event: str | None = None, **kv: Any) -> None:
        self._emit(logging.WARNING, event, **kv)

    def error(self, event: str | None = None, **kv: Any) -> None:
        self._emit(logging.ERROR, event, **kv)

    def debug(self, event: str | None = None, **kv: Any) -> None:
        self._emit(logging.DEBUG, event, **kv)

    def exception(self, event: str | None = None, **kv: Any) -> None:
        extra: dict[str, Any] = {"component": self._component}
        if event:
            extra["event"] = event
        for k, v in kv.items():
            if k in _LOGRECORD_RESERVED:
                extra[f"kv_{k}"] = v
            else:
                extra[k] = v
        self._logger.exception("", extra=extra)


def get_logger(component: str) -> ComponentLogger:
    _ensure_init()
    return ComponentLogger(component, logging.getLogger(f"court.{component}"))
