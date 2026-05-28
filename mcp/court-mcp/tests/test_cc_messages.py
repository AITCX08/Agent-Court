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
