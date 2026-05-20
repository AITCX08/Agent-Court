"""SY-1 (#16): workflow_loader tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import workflow_loader as wl  # noqa: E402


def test_parse_text_with_frontmatter_returns_typed_config():
    text = """---
schema_version: 1
branch_prefix: "feat/auto-"
max_concurrent_runs: 5
run_timeout_seconds: 900
allowed_labels: ["agent-ok", "auto"]
require_approval_stages: ["INTAKE"]
working_dir_strategy: worktree
tracker:
  provider: github
  base_url: https://api.github.com
---

# workflow body

prompt template here {{issue.number}}.
"""
    config, prompt = wl.parse_workflow_text(text)
    assert config.schema_version == 1
    assert config.branch_prefix == "feat/auto-"
    assert config.max_concurrent_runs == 5
    assert config.run_timeout_seconds == 900
    assert config.allowed_labels == ("agent-ok", "auto")
    assert config.require_approval_stages == ("INTAKE",)
    assert config.working_dir_strategy == "worktree"
    assert config.tracker.provider == "github"
    assert config.tracker.base_url == "https://api.github.com"
    assert prompt.startswith("# workflow body")


def test_parse_text_without_frontmatter_uses_all_defaults():
    text = "# just a prompt\n\nno frontmatter at all"
    config, prompt = wl.parse_workflow_text(text)
    assert config.schema_version == 1
    assert config.branch_prefix == "feat/auto-"
    assert config.max_concurrent_runs == 3
    assert config.allowed_labels == ()
    assert config.tracker.provider == "gitea"
    assert prompt == "# just a prompt\n\nno frontmatter at all"


def test_unsupported_schema_version_raises():
    text = "---\nschema_version: 99\n---\nbody\n"
    with pytest.raises(wl.WorkflowParseError, match="unsupported schema_version"):
        wl.parse_workflow_text(text)


def test_invalid_working_dir_strategy_raises():
    text = "---\nworking_dir_strategy: yolo\n---\nbody\n"
    with pytest.raises(wl.WorkflowParseError, match="working_dir_strategy"):
        wl.parse_workflow_text(text)


def test_max_concurrent_zero_raises():
    text = "---\nmax_concurrent_runs: 0\n---\nbody\n"
    with pytest.raises(wl.WorkflowParseError, match="max_concurrent_runs"):
        wl.parse_workflow_text(text)


def test_negative_timeout_raises():
    text = "---\nrun_timeout_seconds: -1\n---\nbody\n"
    with pytest.raises(wl.WorkflowParseError, match="run_timeout_seconds"):
        wl.parse_workflow_text(text)


def test_invalid_tracker_provider_raises():
    text = "---\ntracker:\n  provider: jira\n---\nbody\n"
    with pytest.raises(wl.WorkflowParseError, match="tracker.provider"):
        wl.parse_workflow_text(text)


def test_unknown_frontmatter_fields_are_ignored_forward_compat():
    text = """---
schema_version: 1
future_unknown_field: "xyz"
---
body
"""
    config, _ = wl.parse_workflow_text(text)
    assert config.branch_prefix == "feat/auto-"  # defaults preserved


def test_bad_yaml_frontmatter_raises_parse_error():
    text = "---\n:: not valid yaml ::\n---\nbody\n"
    with pytest.raises(wl.WorkflowParseError, match="YAML"):
        wl.parse_workflow_text(text)


def test_allowed_labels_not_a_list_raises():
    text = "---\nallowed_labels: \"agent-ok\"\n---\nbody\n"
    with pytest.raises(wl.WorkflowParseError, match="allowed_labels"):
        wl.parse_workflow_text(text)


def test_load_workflow_prefers_workflow_md_over_skill_md(tmp_path: Path):
    skill_dir = tmp_path / ".claude" / "skills" / "issue-resolver"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# legacy SKILL.md\nfallback prompt\n")
    (tmp_path / "WORKFLOW.md").write_text(
        "---\nschema_version: 1\nbranch_prefix: 'fresh/'\n---\n# fresh workflow\n"
    )
    wf = wl.load_workflow(tmp_path)
    assert wf.config.branch_prefix == "fresh/"
    assert wf.source_path.name == "WORKFLOW.md"
    assert "fresh workflow" in wf.prompt_template


def test_load_workflow_falls_back_to_skill_md(tmp_path: Path):
    skill_dir = tmp_path / ".claude" / "skills" / "issue-resolver"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# legacy\nfallback body\n")
    wf = wl.load_workflow(tmp_path)
    assert wf.source_path.name == "SKILL.md"
    assert "fallback body" in wf.prompt_template
    # 默认 config
    assert wf.config.branch_prefix == "feat/auto-"


def test_load_workflow_missing_raises_not_found(tmp_path: Path):
    with pytest.raises(wl.WorkflowNotFoundError):
        wl.load_workflow(tmp_path)


def test_render_substitutes_template_variables():
    wf = wl.Workflow(
        config=wl.WorkflowConfig(),
        prompt_template="处理 {{issue.repo}}#{{issue.number}}: {{issue.title}}",
        source_path=Path("/dev/null"),
    )
    out = wf.render(issue={"repo": "foo/bar", "number": 42, "title": "fix bug"})
    assert out == "处理 foo/bar#42: fix bug"


def test_render_with_none_field_renders_empty_string():
    wf = wl.Workflow(
        config=wl.WorkflowConfig(),
        prompt_template="title={{issue.title}}/",
        source_path=Path("/dev/null"),
    )
    out = wf.render(issue={"title": None})
    assert out == "title=/"


def test_render_without_issue_arg_leaves_template_intact():
    wf = wl.Workflow(
        config=wl.WorkflowConfig(),
        prompt_template="static {{issue.repo}}",
        source_path=Path("/dev/null"),
    )
    assert wf.render() == "static {{issue.repo}}"
