"""Tests for cc_messages (PR-21 dashboard comm tab)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cc_messages import (  # noqa: E402
    Message,
    parse_session_file,
)


FIXTURE_K2WORK = {
    "sessions": {
        "s1": {
            "id": "s1",
            "name": "default",
            "agent_session_id": "abc-123",
            "agent_type": "claudecode",
            "history": [
                {"role": "user", "content": "你好", "timestamp": "2026-05-09T20:24:46.426121+08:00"},
                {"role": "assistant", "content": "你好!", "timestamp": "2026-05-09T20:24:53.877704+08:00"},
            ],
            "created_at": "2026-05-09T20:24:46.426121+08:00",
            "updated_at": "2026-05-09T20:24:53.877704+08:00",
        }
    },
    "active_session": {
        "weixin:dm:user-xxx@im.wechat": "s1"
    },
    "user_sessions": {"weixin:dm:user-xxx@im.wechat": ["s1"]},
    "version": 1,
}


def test_message_dataclass_shape():
    m = Message(
        platform="weixin",
        session_key="weixin:dm:user-xxx@im.wechat",
        session_id="s1",
        project="k2work",
        role="user",
        content="你好",
        timestamp="2026-05-09T20:24:46.426121+08:00",
        msg_id="k2work:s1:0",
    )
    assert m.platform == "weixin"
    assert m.role == "user"
    assert m.msg_id == "k2work:s1:0"


def test_parse_session_file_extracts_messages(tmp_path):
    fp = tmp_path / "k2work_abc.json"
    fp.write_text(json.dumps(FIXTURE_K2WORK), encoding="utf-8")
    msgs = parse_session_file(fp, project="k2work")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "你好"
    assert msgs[0].platform == "weixin"  # 从 active_session 的 key 提取
    assert msgs[0].session_key == "weixin:dm:user-xxx@im.wechat"
    assert msgs[0].session_id == "s1"
    assert msgs[0].project == "k2work"
    assert msgs[0].msg_id == "k2work:s1:0"
    assert msgs[1].role == "assistant"
    assert msgs[1].msg_id == "k2work:s1:1"


def test_parse_session_file_missing_active_session_falls_back_to_unknown(tmp_path):
    data = dict(FIXTURE_K2WORK)
    data["active_session"] = {}
    fp = tmp_path / "k2work_abc.json"
    fp.write_text(json.dumps(data), encoding="utf-8")
    msgs = parse_session_file(fp, project="k2work")
    assert len(msgs) == 2
    assert msgs[0].platform == "unknown"


def test_parse_session_file_corrupt_json_returns_empty(tmp_path):
    fp = tmp_path / "broken.json"
    fp.write_text("not json", encoding="utf-8")
    msgs = parse_session_file(fp, project="anything")
    assert msgs == []


from cc_messages import list_messages  # noqa: E402


def _write_session(tmp_path, project, sid, history, active_key="weixin:dm:u@h"):
    fp = tmp_path / f"{project}_abc.json"
    fp.write_text(json.dumps({
        "sessions": {sid: {"id": sid, "history": history}},
        "active_session": {active_key: sid},
        "version": 1,
    }), encoding="utf-8")
    return fp


def test_list_messages_returns_all_sorted_by_timestamp_desc(tmp_path, monkeypatch):
    _write_session(tmp_path, "k2work", "s1", [
        {"role": "user", "content": "A", "timestamp": "2026-05-10T10:00:00+08:00"},
        {"role": "assistant", "content": "B", "timestamp": "2026-05-10T10:00:05+08:00"},
    ])
    _write_session(tmp_path, "persona", "s1", [
        {"role": "user", "content": "C", "timestamp": "2026-05-10T10:00:03+08:00"},
    ], active_key="feishu:dm:u@feishu")

    monkeypatch.setattr("cc_messages._resolve_sessions_dir", lambda: tmp_path)

    msgs = list_messages(limit=10)
    assert [m.content for m in msgs] == ["B", "C", "A"]
    assert msgs[1].project == "persona"
    assert msgs[1].platform == "feishu"


def test_list_messages_respects_limit(tmp_path, monkeypatch):
    _write_session(tmp_path, "k2work", "s1", [
        {"role": "user", "content": f"M{i}", "timestamp": f"2026-05-10T10:00:{i:02d}+08:00"}
        for i in range(10)
    ])
    monkeypatch.setattr("cc_messages._resolve_sessions_dir", lambda: tmp_path)

    msgs = list_messages(limit=3)
    assert len(msgs) == 3
    assert [m.content for m in msgs] == ["M9", "M8", "M7"]


def test_list_messages_before_cursor(tmp_path, monkeypatch):
    _write_session(tmp_path, "k2work", "s1", [
        {"role": "user", "content": f"M{i}", "timestamp": f"2026-05-10T10:00:{i:02d}+08:00"}
        for i in range(5)
    ])
    monkeypatch.setattr("cc_messages._resolve_sessions_dir", lambda: tmp_path)

    msgs = list_messages(limit=10, before="2026-05-10T10:00:03+08:00")
    assert [m.content for m in msgs] == ["M2", "M1", "M0"]


def test_list_messages_empty_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("cc_messages._resolve_sessions_dir", lambda: tmp_path)
    msgs = list_messages(limit=10)
    assert msgs == []


import time as _time

from cc_messages import subscribe  # noqa: E402


def test_subscribe_emits_new_messages_on_file_change(tmp_path, monkeypatch):
    """文件原 2 条 → modify 成 4 条 → callback 收到 2 条新消息。"""
    fp = _write_session(tmp_path, "k2work", "s1", [
        {"role": "user", "content": "A", "timestamp": "2026-05-10T10:00:00+08:00"},
        {"role": "assistant", "content": "B", "timestamp": "2026-05-10T10:00:05+08:00"},
    ])
    monkeypatch.setattr("cc_messages._resolve_sessions_dir", lambda: tmp_path)

    received: list[Message] = []
    stop = subscribe(callback=lambda m: received.append(m))

    try:
        _time.sleep(0.3)
        fp.write_text(json.dumps({
            "sessions": {"s1": {"id": "s1", "history": [
                {"role": "user", "content": "A", "timestamp": "2026-05-10T10:00:00+08:00"},
                {"role": "assistant", "content": "B", "timestamp": "2026-05-10T10:00:05+08:00"},
                {"role": "user", "content": "C", "timestamp": "2026-05-10T10:00:10+08:00"},
                {"role": "assistant", "content": "D", "timestamp": "2026-05-10T10:00:15+08:00"},
            ]}},
            "active_session": {"weixin:dm:u@h": "s1"},
            "version": 1,
        }), encoding="utf-8")
        _time.sleep(0.8)
    finally:
        stop()

    contents = [m.content for m in received]
    assert "C" in contents
    assert "D" in contents
    assert "A" not in contents
    assert "B" not in contents
