"""SY-5 #15: structured JSON logging tests."""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest

# 让 pytest 从 mcp/court-mcp 找模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import log as logmod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_log():
    logmod.reset_for_testing()
    yield
    logmod.reset_for_testing()


def _attach_capture() -> io.StringIO:
    """init 之后, 把 stderr handler 的 stream 替换成 StringIO 抓取输出."""
    logmod.get_logger("test_setup")  # 触发 init
    root = logging.getLogger("court")
    cap = io.StringIO()
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.stream = cap
    return cap


def _lines(cap: io.StringIO) -> list[dict]:
    return [json.loads(ln) for ln in cap.getvalue().splitlines() if ln.strip()]


def test_basic_info_event_with_kv():
    cap = _attach_capture()
    log = logmod.get_logger("watcher")
    log.info(event="issue_new", repo="foo/bar", num=12, source="webhook")
    [entry] = _lines(cap)
    assert entry["level"] == "info"
    assert entry["component"] == "watcher"
    assert entry["event"] == "issue_new"
    assert entry["repo"] == "foo/bar"
    assert entry["num"] == 12
    assert entry["source"] == "webhook"
    assert "ts" in entry and entry["ts"].endswith("Z")


def test_warning_and_error_levels_render_correctly():
    cap = _attach_capture()
    log = logmod.get_logger("approval")
    log.warning(event="notify_failed", channel="wechat")
    log.error(event="dispatch_failed", repo="a/b", num=3, error="boom")
    entries = _lines(cap)
    assert [e["level"] for e in entries] == ["warning", "error"]
    assert entries[0]["event"] == "notify_failed"
    assert entries[1]["error"] == "boom"


def test_no_event_no_kv_emits_minimal_record():
    cap = _attach_capture()
    log = logmod.get_logger("legacy")
    log.info()  # 不带任何参数也得到合法 JSON
    [entry] = _lines(cap)
    assert entry["level"] == "info"
    assert entry["component"] == "legacy"
    assert "event" not in entry


def test_caller_uses_reserved_kv_name_is_namespaced():
    cap = _attach_capture()
    log = logmod.get_logger("router")
    # ``message`` / ``name`` / ``filename`` 都是 LogRecord 保留字段名,
    # 自动改名为 kv_<original>, 不丢失数据也不污染输出
    log.info(event="im_in", message="this would clash", name="alt")
    [entry] = _lines(cap)
    assert entry["kv_message"] == "this would clash"
    assert entry["kv_name"] == "alt"
    assert entry["event"] == "im_in"


def test_idempotent_init_does_not_duplicate_handlers():
    logmod.get_logger("a")
    logmod.get_logger("b")
    logmod.get_logger("c")
    root = logging.getLogger("court")
    # 只应该有 1 个 stderr handler (没设 file sink 时)
    handlers = [h for h in root.handlers if not isinstance(h, logging.FileHandler)]
    assert len(handlers) == 1


def test_file_sink_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log_file = tmp_path / "court.log"
    monkeypatch.setenv(logmod.ENV_LOG_FILE, str(log_file))
    cap = _attach_capture()
    log = logmod.get_logger("watcher")
    log.info(event="issue_new", num=42)
    log.error(event="dispatch_failed", num=42, error="x")
    # stderr 和 file 都应该有 2 条
    stderr_entries = _lines(cap)
    assert len(stderr_entries) == 2
    file_entries = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    assert len(file_entries) == 2
    assert file_entries[0]["event"] == "issue_new"
    assert file_entries[1]["error"] == "x"


def test_grep_a_single_issue_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SY-5 验收: tail file | jq 'select(.num==12)' 拉得出 issue 12 的全部日志."""
    log_file = tmp_path / "court.log"
    monkeypatch.setenv(logmod.ENV_LOG_FILE, str(log_file))
    _attach_capture()
    w = logmod.get_logger("watcher")
    r = logmod.get_logger("router")
    a = logmod.get_logger("approval")
    # 模拟 issue 12 全生命周期
    w.info(event="issue_new", num=12, repo="foo/bar", source="webhook")
    r.info(event="dispatched", num=12, window="foo-bar-12")
    a.info(event="approved", num=12, winner="terminal", stage="INTAKE")
    # 同时夹一条 issue 99 的日志, 验证 grep 能精确隔离
    w.info(event="issue_new", num=99, repo="x/y", source="polling")

    file_entries = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    issue_12 = [e for e in file_entries if e.get("num") == 12]
    assert len(issue_12) == 3
    assert [e["event"] for e in issue_12] == ["issue_new", "dispatched", "approved"]
    assert [e["component"] for e in issue_12] == ["watcher", "router", "approval"]
