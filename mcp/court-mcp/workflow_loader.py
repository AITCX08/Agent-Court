"""SY-1 (#16) WORKFLOW.md in-repo 加载器.

把 agent 行为配置 (超时 / 分支前缀 / 并发上限 / 允许 label / 审批阶段) 跟
prompt 一起 commit 进项目根 ``WORKFLOW.md``, 让团队像 review 代码一样 review
agent 行为. 借鉴 [openai/symphony](https://github.com/openai/symphony) SPEC
§3.1.1 (Workflow Loader).

文件格式::

    ---
    schema_version: 1
    branch_prefix: "feat/auto-"
    max_concurrent_runs: 3
    ...
    ---

    # agent-court workflow

    你是 agent-court ... (prompt body)

调用约定::

    from workflow_loader import load_workflow
    wf = load_workflow(repo_root)
    wf.config.branch_prefix         # → "feat/auto-"
    wf.prompt_template              # → "# agent-court workflow\n\n你是..."
    wf.render(issue={"repo": ..., "number": ..., "title": ...})

如果根目录没 WORKFLOW.md, fallback 读 ``.claude/skills/issue-resolver/SKILL.md``
(向后兼容; 1 个版本周期后会下线).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

WORKFLOW_FILE_CANDIDATES: tuple[str, ...] = (
    "WORKFLOW.md",
    ".claude/skills/issue-resolver/SKILL.md",  # 向后兼容 fallback
)

SUPPORTED_SCHEMA_VERSIONS = {1}
DEFAULT_TRACKER_PROVIDER = "gitea"


class WorkflowNotFoundError(FileNotFoundError):
    """``WORKFLOW.md`` 跟 fallback 都不存在."""


class WorkflowParseError(ValueError):
    """frontmatter YAML 解析失败 / schema 不支持 / 字段类型错."""


@dataclass(frozen=True)
class TrackerConfig:
    provider: str = DEFAULT_TRACKER_PROVIDER  # gitea | github (BOOT-1 启用 github)
    base_url: str | None = None  # None = 用 provider 默认


@dataclass(frozen=True)
class WorkflowConfig:
    """所有配置都有默认值; ``WORKFLOW.md`` frontmatter 缺字段不算错."""

    schema_version: int = 1
    branch_prefix: str = "feat/auto-"
    max_concurrent_runs: int = 3
    run_timeout_seconds: int = 1800
    retry_max: int = 3
    retry_backoff_base_seconds: int = 60
    working_dir_strategy: str = "inplace"  # inplace | worktree (SY-2 启用)
    allowed_labels: tuple[str, ...] = ()  # 空 = 不过滤; 非空 = issue 必须命中一个
    require_approval_stages: tuple[str, ...] = ("INTAKE", "PLAN")
    tracker: TrackerConfig = field(default_factory=TrackerConfig)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkflowConfig":
        # 拷一份避免污染外部
        data = dict(raw)
        # 类型 / enum 校验
        if "schema_version" in data and data["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
            raise WorkflowParseError(
                f"unsupported schema_version: {data['schema_version']!r}; "
                f"loader 支持 {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        if "working_dir_strategy" in data and data["working_dir_strategy"] not in {"inplace", "worktree"}:
            raise WorkflowParseError(
                f"working_dir_strategy must be 'inplace' or 'worktree', got {data['working_dir_strategy']!r}"
            )
        for k in ("allowed_labels", "require_approval_stages"):
            if k in data:
                v = data[k]
                if v is None:
                    data[k] = ()
                elif isinstance(v, list):
                    if not all(isinstance(x, str) for x in v):
                        raise WorkflowParseError(f"{k} 必须是 list of str, got {v!r}")
                    data[k] = tuple(v)
                else:
                    raise WorkflowParseError(f"{k} 必须是 list, got {type(v).__name__}")
        for k in ("max_concurrent_runs", "run_timeout_seconds", "retry_max", "retry_backoff_base_seconds"):
            if k in data and not isinstance(data[k], int):
                raise WorkflowParseError(f"{k} 必须是 int, got {type(data[k]).__name__}")
            if k in data and data[k] < 0:
                raise WorkflowParseError(f"{k} 不能为负, got {data[k]!r}")
        if data.get("max_concurrent_runs") == 0:
            raise WorkflowParseError("max_concurrent_runs 必须 > 0")
        # tracker 嵌套
        tracker_raw = data.pop("tracker", None)
        if tracker_raw is None:
            tracker = TrackerConfig()
        elif isinstance(tracker_raw, dict):
            provider = tracker_raw.get("provider", DEFAULT_TRACKER_PROVIDER)
            if provider not in {"gitea", "github"}:
                raise WorkflowParseError(f"tracker.provider must be 'gitea' or 'github', got {provider!r}")
            tracker = TrackerConfig(
                provider=provider,
                base_url=tracker_raw.get("base_url"),
            )
        else:
            raise WorkflowParseError(f"tracker 必须是 dict, got {type(tracker_raw).__name__}")
        # 只保留已知字段; 未知字段忽略 (前向兼容)
        known = {f.name for f in fields(cls)} - {"tracker"}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(tracker=tracker, **clean)


@dataclass(frozen=True)
class Workflow:
    config: WorkflowConfig
    prompt_template: str
    source_path: Path  # 哪个文件被加载的 (诊断用)

    def render(self, *, issue: dict[str, Any] | None = None) -> str:
        """简单模板替换: ``{{issue.repo}}`` / ``{{issue.number}}`` / ``{{issue.title}}``.

        不支持复杂逻辑 (no Jinja). 设计哲学: prompt 模板本身已经够长, 让它继续
        当人类可读的 markdown, 模板变量只用最简单的 dot-path 替换.
        """
        out = self.prompt_template
        if issue is not None:
            for k, v in _flatten("issue", issue).items():
                out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
        return out


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_workflow_text(text: str) -> tuple[WorkflowConfig, str]:
    """从单个 ``WORKFLOW.md`` 文本里拆出 (config, prompt_template).

    frontmatter 可选; 没有 frontmatter 时整个 text 都是 prompt, config 走默认.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return WorkflowConfig(), text.lstrip()
    raw_frontmatter, body = m.group(1), m.group(2)
    try:
        data = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"frontmatter YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowParseError(f"frontmatter 顶层必须是 mapping, got {type(data).__name__}")
    config = WorkflowConfig.from_dict(data)
    return config, body.lstrip()


def load_workflow(repo_root: Path) -> Workflow:
    """从 ``repo_root`` 找 WORKFLOW.md (或 fallback SKILL.md), 返 Workflow.

    优先 WORKFLOW.md → fallback ``.claude/skills/issue-resolver/SKILL.md``;
    都不存在抛 ``WorkflowNotFoundError``.
    """
    for rel in WORKFLOW_FILE_CANDIDATES:
        candidate = repo_root / rel
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            config, prompt = parse_workflow_text(text)
            return Workflow(config=config, prompt_template=prompt, source_path=candidate)
    raise WorkflowNotFoundError(
        f"找不到 WORKFLOW.md (or fallback SKILL.md) under {repo_root}; "
        f"候选: {', '.join(WORKFLOW_FILE_CANDIDATES)}"
    )


def _flatten(prefix: str, value: Any) -> dict[str, Any]:
    """``{"repo": "foo/bar", "number": 12}`` 配合 prefix=issue → {issue.repo, issue.number}."""
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, dict):
            out.update(_flatten(f"{prefix}.{k}", v))
        else:
            out[f"{prefix}.{k}"] = v
    return out
