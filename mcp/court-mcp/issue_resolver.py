from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import seen_state
from dual_channel_approval import request_plan


def build_intro_message(issue_detail: dict[str, Any], comments: list[dict[str, Any]], decision: dict[str, Any]) -> str:
    repo = issue_detail["repository"]["full_name"]
    number = int(issue_detail["number"])
    comment_excerpt = comments[:20]
    return "\n".join([
        f"ISSUE_RESOLVER_BEGIN {repo} {number}",
        "--- issue.json ---",
        json.dumps(issue_detail, ensure_ascii=False, indent=2),
        "--- comments.json ---",
        json.dumps(comment_excerpt, ensure_ascii=False, indent=2),
        "--- shenli.decision.json ---",
        json.dumps(decision, ensure_ascii=False, indent=2),
    ]) + "\n"


def report_back(repo: str, num: int, summary: str, stage: str = "done", *, comment: bool = True) -> dict[str, Any]:
    """更新 seen-issues.json 状态. stage=executing|done.

    done 时若 comment=True 会调 GiteaClient 在 issue 上发完成汇报评论.
    SY-2 #19: done 时若 WORKFLOW.md 是 working_dir_strategy=worktree, 自动
    cleanup 该 issue 的 worktree (释放 ~/.agent-court/worktrees/<safe_key>/).
    """
    if stage not in {"executing", "done"}:
        return {"ok": False, "reason": f"unsupported stage: {stage!r}"}

    last_action = "EXECUTING" if stage == "executing" else "DONE_DASHBOARD"
    patch: dict[str, Any] = {
        "last_action": last_action,
        "stage": stage.upper(),
        "summary": summary,
    }
    entry = seen_state.update_entry(repo, num, patch)
    if not entry:
        return {"ok": False, "reason": "issue missing in seen-issues.json"}

    result: dict[str, Any] = {"ok": True}

    if stage == "done" and comment:
        try:
            from gitea_client import GiteaClient

            winner = entry.get("approval_winner", "?")
            body = f"## ✅ dashboard 处理完成\n\nwinner: {winner}\n\n{summary}"
            GiteaClient().comment_on_issue(repo, num, body)
        except Exception as exc:  # pragma: no cover - defensive
            result["comment"] = False
            result["comment_error"] = str(exc)

    if stage == "done":
        worktree_cleaned = _cleanup_worktree_if_configured(repo, num)
        if worktree_cleaned is not None:
            result["worktree_cleaned"] = worktree_cleaned

    return result


def _cleanup_worktree_if_configured(repo: str, num: int) -> bool | None:
    """SY-2 #19: 完成时清掉本 issue 的 git worktree (如果用了 worktree 策略).

    返回:
    - True: 真的清了 worktree
    - False: 配置了 worktree 但 cleanup 时已不存在 (上次清过 / 没创建)
    - None: WORKFLOW.md 没配 worktree 策略 (inplace 模式) / 模块缺失 / 推断失败
    """
    try:
        from workflow_loader import load_workflow
        from workspace_manager import WorkspaceManager
    except ImportError:
        return None
    repo_root = Path(__file__).resolve().parents[2]
    try:
        wf = load_workflow(repo_root)
    except Exception:
        return None
    if wf.config.working_dir_strategy != "worktree":
        return None
    repo_name = repo.split("/", 1)[1] if "/" in repo else repo
    source_repo = Path.home() / "Desktop" / "K2Work" / repo_name
    if not source_repo.is_dir():
        return None
    try:
        return WorkspaceManager(branch_prefix=wf.config.branch_prefix).cleanup_worktree(
            source_repo, f"{repo}#{num}"
        )
    except Exception:  # pragma: no cover - defensive
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m issue_resolver")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("request-plan")
    plan_parser.add_argument("--repo", required=True)
    plan_parser.add_argument("--num", required=True, type=int)
    plan_parser.add_argument("--plan-file", required=True)
    plan_parser.add_argument("--window", required=True)

    report_parser = sub.add_parser("report-back")
    report_parser.add_argument("--repo", required=True)
    report_parser.add_argument("--num", required=True, type=int)
    report_parser.add_argument("--summary-file", required=True)
    report_parser.add_argument("--stage", default="done", choices=["executing", "done"])
    report_parser.add_argument("--no-comment", action="store_true", help="完成时不发 issue 评论 (默认发)")

    args = parser.parse_args(argv)
    if args.command == "request-plan":
        verdict = request_plan(args.repo, args.num, Path(args.plan_file).read_text(), args.window)
        print(json.dumps(verdict.__dict__, ensure_ascii=False))
        return 0
    if args.command == "report-back":
        result = report_back(
            args.repo,
            args.num,
            Path(args.summary_file).read_text(),
            stage=args.stage,
            comment=not args.no_comment,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
