"""PR-15 court-dashboard 后端 HTTP server (aiohttp).

只 bind 127.0.0.1, 用随机 token 校验; 前端首次拿 URL ``?t=<token>`` 入门,
拿到后通过 ``Authorization: Bearer <token>`` 调 API. SSE 走 ``?t=`` (EventSource
不支持 header).

启动套路 (signal handlers / logging / OSError 退出码 4) 复用 PR-14
``gitea_webhook_receiver.py`` 的写法.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from agent_spawn import AgentSpawner, SpawnError
from agent_teams import AgentTeamAggregator
from dashboard_aggregator import (
    CACHE_TTL_SECONDS,
    DashboardAggregator,
    FsWatcher,
)
from dashboard_tmux import SESSION_NAME as DASHBOARD_SESSION
from dual_channel_approval import submit_verdict as approval_submit_verdict
from gitea_client import GiteaClientError
from git_board import GitBoardAggregator, list_scopes
from team_links import TeamLinks
from log import get_logger
from orchestrator import Orchestrator
from seen_state import default_state_dir

VERSION = "pr-15"
SSE_KEEPALIVE_SECONDS = 30
FRONTEND_DIST_DIRNAME = "frontend/dist"

_log = get_logger("dashboard-server")


# ---------------------------------------------------------------------------
# app 工厂
# ---------------------------------------------------------------------------


def create_app(
    *,
    token: str,
    state_dir: Path | None = None,
    frontend_dist: Path | None = None,
    fs_watcher_enabled: bool = True,
) -> web.Application:
    if not token:
        raise ValueError("token must be non-empty")

    resolved_state_dir = state_dir or default_state_dir()
    team_links = TeamLinks(court_root=resolved_state_dir.parent)
    agent_spawner = AgentSpawner(team_links=team_links)
    orchestrator = Orchestrator(court_root=resolved_state_dir.parent)
    aggregator = DashboardAggregator(state_dir=state_dir, orchestrator=orchestrator)
    git_board = GitBoardAggregator(team_links=team_links)
    agent_teams = AgentTeamAggregator(court_root=resolved_state_dir.parent, team_links=team_links)
    app = web.Application(middlewares=[_token_middleware(token)])
    app["token"] = token
    app["aggregator"] = aggregator
    app["state_dir"] = resolved_state_dir
    app["orchestrator"] = orchestrator
    app["git_board"] = git_board
    app["agent_teams"] = agent_teams
    app["team_links"] = team_links
    app["agent_spawner"] = agent_spawner
    app["frontend_dist"] = frontend_dist or _default_frontend_dist()
    app["fs_watcher_enabled"] = fs_watcher_enabled
    app["fs_watcher"] = None

    app.router.add_get("/api/healthz", handle_healthz)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/orchestrator/snapshot", handle_orchestrator_snapshot)
    app.router.add_get("/api/git-board", handle_git_board)
    app.router.add_post("/api/git-board/refresh", handle_git_board_refresh)
    app.router.add_get("/api/agent-teams", handle_agent_teams)
    app.router.add_post("/api/agent/team-label", handle_agent_team_label)
    app.router.add_post("/api/agent/spawn", handle_agent_spawn)
    app.router.add_delete("/api/agent/{team_id}", handle_agent_kill)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/approve", handle_approve)
    app.router.add_post("/api/reject", handle_reject)
    app.router.add_post("/api/kill", handle_kill)
    _add_static_routes(app)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def _default_frontend_dist() -> Path:
    """``mcp/court-mcp/`` 上溯到仓库根, 再拼 ``frontend/dist``."""
    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent
    return repo_root / FRONTEND_DIST_DIRNAME


async def _on_startup(app: web.Application) -> None:
    if not app["fs_watcher_enabled"]:
        return
    aggregator: DashboardAggregator = app["aggregator"]
    fw = FsWatcher(app["state_dir"], aggregator.emit_change)
    try:
        fw.start()
    except Exception as exc:
        _log.warning(event="fs_watcher_start_failed", error=repr(exc))
        return
    app["fs_watcher"] = fw


async def _on_cleanup(app: web.Application) -> None:
    fw = app.get("fs_watcher")
    if fw is not None:
        fw.stop()
        app["fs_watcher"] = None


# ---------------------------------------------------------------------------
# T-15-05: token middleware
# ---------------------------------------------------------------------------

_PUBLIC_PATHS = {"/api/healthz"}
_PUBLIC_PATH_PREFIXES = ("/assets/",)


def _is_public(path: str) -> bool:
    """``/`` index.html 仍需校 token (走 _is_browser_index 提示页);
    ``/assets/*`` 是 build 产物 (公开 npm 仓库就能复刻), 跳过 token 校验,
    否则 ``<script src='./assets/...'>`` 无 query 跟随时被 401, JS 加载不到, 页面空白."""
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES)


def _token_middleware(expected: str):
    @web.middleware
    async def middleware(request: web.Request, handler: Callable[..., Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if _is_public(request.path):
            return await handler(request)
        provided = _extract_token(request)
        if provided is None or not secrets.compare_digest(provided, expected):
            if _is_browser_index(request):
                return _render_unauthorized_index()
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)
    return middleware


def _extract_token(request: web.Request) -> str | None:
    qt = request.rel_url.query.get("t")
    if qt:
        return qt
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() or None
    return None


def _is_browser_index(request: web.Request) -> bool:
    if request.path != "/" or request.method != "GET":
        return False
    return "text/html" in request.headers.get("Accept", "")


def _render_unauthorized_index() -> web.Response:
    # SY-3 v2 修: 顶层导航不会带 localStorage 里的 token (PR-15 原 UX bug —
    # 首访剥掉 ?t= 后刷新就 401). 这里塞段 JS, localStorage 有 token 时
    # 自动重定向回 ?t=<token>; 没 token 才显示静态提示页.
    html = """<!doctype html><html><head><meta charset="utf-8"><title>court-dashboard</title>
<script>(function(){
  try {
    var t = localStorage.getItem('court-dashboard-token');
    if (t) {
      var u = new URL(window.location.href);
      u.searchParams.set('t', t);
      window.location.replace(u.toString());
      return;
    }
  } catch (_) {}
})();</script>
<style>body{font:14px ui-sans-serif,system-ui;background:#0b0d10;color:#cdd6dd;padding:48px;}
code{background:#1a1f25;color:#a5d4ff;padding:2px 6px;border-radius:4px;}
.box{max-width:560px;border:1px solid #2a3239;padding:24px;border-radius:8px;background:#10141a;}
h1{margin-top:0;color:#fff;}</style></head>
<body><div class="box"><h1>401 unauthorized</h1>
<p>court-dashboard 需要带 token. 启动时会把带 token 的链接打印到终端
(看 <code>bin/court-dashboard</code> 输出).</p>
<p>或者手动: <code>?t=&lt;your-token&gt;</code></p></div></body></html>"""
    return web.Response(text=html, status=401, content_type="text/html")


# ---------------------------------------------------------------------------
# T-15-06: /api/healthz + /api/status
# ---------------------------------------------------------------------------


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": VERSION})


async def handle_status(request: web.Request) -> web.Response:
    aggregator: DashboardAggregator = request.app["aggregator"]
    snapshot = await aggregator.aggregate_status()
    return web.json_response(snapshot)


async def handle_orchestrator_snapshot(request: web.Request) -> web.Response:
    """SY-3 (#18): 旁挂只读视图. 不替换 /api/status, 跟它并行 (v1 边界)."""
    orchestrator: Orchestrator = request.app["orchestrator"]
    snap = await asyncio.to_thread(orchestrator.snapshot)
    return web.json_response(snap.to_dict())


# ---------------------------------------------------------------------------
# PR-16b: Git Board (Gitea 聚合)
# ---------------------------------------------------------------------------


async def handle_git_board(request: web.Request) -> web.Response:
    scope = request.rel_url.query.get("scope", "related")
    if scope not in list_scopes():
        return web.json_response(
            {"error": "invalid_scope", "scope": scope, "valid": list_scopes()},
            status=400,
        )
    board: GitBoardAggregator = request.app["git_board"]
    try:
        result = await board.get_board(scope)
    except GiteaClientError as exc:
        return web.json_response(
            {"error": "gitea_error", "detail": str(exc), "scope": scope},
            status=502,
        )
    return web.json_response(result)


async def handle_git_board_refresh(request: web.Request) -> web.Response:
    body = await _read_json(request) or {}
    scope = body.get("scope") if isinstance(body, dict) else None
    board: GitBoardAggregator = request.app["git_board"]
    board.invalidate(scope if isinstance(scope, str) else None)
    return web.json_response({"ok": True, "scope": scope or "all"})


# ---------------------------------------------------------------------------
# PR-17a: Agent Teams (ghostty + tmux 聚合 + label)
# ---------------------------------------------------------------------------


async def handle_agent_teams(request: web.Request) -> web.Response:
    aggregator: AgentTeamAggregator = request.app["agent_teams"]
    snap = await asyncio.to_thread(aggregator.snapshot)
    return web.json_response(snap)


_TEAM_ID_RE = re.compile(r"^(ghostty|tmux):[A-Za-z0-9._\-]+$")


async def handle_agent_team_label(request: web.Request) -> web.Response:
    body = await _read_json(request)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_json"}, status=400)
    team_id = body.get("id")
    if not isinstance(team_id, str) or not _TEAM_ID_RE.match(team_id):
        return web.json_response(
            {"error": "invalid_team_id", "id": team_id,
             "hint": "must match (ghostty|tmux):<alnum>"},
            status=400,
        )
    label = body.get("label", "")
    if not isinstance(label, str):
        return web.json_response({"error": "invalid_label"}, status=400)
    cli = body.get("cli", "")
    started_at = body.get("started_at", "")
    if not isinstance(cli, str) or not isinstance(started_at, str):
        return web.json_response({"error": "invalid_cli_or_started_at"}, status=400)
    aggregator: AgentTeamAggregator = request.app["agent_teams"]
    result = await asyncio.to_thread(
        aggregator.set_label, team_id, label.strip(), cli=cli, started_at=started_at
    )
    return web.json_response({"ok": True, **result})


async def handle_agent_spawn(request: web.Request) -> web.Response:
    body = await _read_json(request)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_json"}, status=400)
    repo = body.get("repo")
    number = body.get("number")
    kind = body.get("kind", "pr")
    url = body.get("url", "")
    if not isinstance(repo, str) or "/" not in repo:
        return web.json_response({"error": "invalid_repo"}, status=400)
    if not isinstance(number, int) or number <= 0:
        return web.json_response({"error": "invalid_number"}, status=400)
    if kind not in ("pr", "issue"):
        return web.json_response({"error": "invalid_kind"}, status=400)
    if not isinstance(url, str):
        return web.json_response({"error": "invalid_url"}, status=400)
    spawner: AgentSpawner = request.app["agent_spawner"]
    try:
        result = await asyncio.to_thread(
            spawner.spawn, repo=repo, number=int(number), kind=kind, url=url,
        )
    except SpawnError as exc:
        return web.json_response({"error": "spawn_failed", "detail": str(exc)}, status=500)
    # spawn 后 git-board cache 可能过期 (linked_team 字段会变), 让前端 refresh 取最新
    request.app["git_board"].invalidate()
    return web.json_response({"ok": True, **result})


async def handle_agent_kill(request: web.Request) -> web.Response:
    team_id = request.match_info.get("team_id", "")
    # body must have confirm: true
    body = await _read_json(request) or {}
    if body.get("confirm") is not True:
        return web.json_response({"error": "confirm_required"}, status=400)
    spawner: AgentSpawner = request.app["agent_spawner"]
    try:
        result = await asyncio.to_thread(spawner.kill, team_id)
    except ValueError as exc:
        return web.json_response({"error": "invalid_team_id", "detail": str(exc)}, status=400)
    except SpawnError as exc:
        return web.json_response({"error": "kill_failed", "detail": str(exc)}, status=500)
    # invalidate git_board cache so linked_team disappears
    request.app["git_board"].invalidate()
    return web.json_response(result)


# ---------------------------------------------------------------------------
# T-15-07: /api/events (SSE)
# ---------------------------------------------------------------------------


async def handle_events(request: web.Request) -> web.StreamResponse:
    aggregator: DashboardAggregator = request.app["aggregator"]
    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    queue = aggregator.subscribe()
    try:
        # 初连立刻 push 一次完整状态
        initial = await aggregator.aggregate_status()
        await _sse_send(response, initial)
        while not request.transport.is_closing():
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
                continue
            # emit_change 已 invalidate cache; 推最新 snapshot 而不是 raw payload
            snapshot = await aggregator.aggregate_status()
            await _sse_send(response, snapshot)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        aggregator.unsubscribe(queue)
    return response


async def _sse_send(response: web.StreamResponse, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    await response.write(f"data: {body}\n\n".encode("utf-8"))


# ---------------------------------------------------------------------------
# T-15-08: approve / reject / kill
# ---------------------------------------------------------------------------


async def handle_approve(request: web.Request) -> web.Response:
    return await _handle_verdict(request, verdict="approve")


async def handle_reject(request: web.Request) -> web.Response:
    return await _handle_verdict(request, verdict="reject")


async def _handle_verdict(request: web.Request, *, verdict: str) -> web.Response:
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid_json"}, status=400)
    slug = body.get("slug_id") or body.get("verdict_id")
    repo = body.get("repo")
    number = body.get("number")
    stage = body.get("stage")
    if not all(isinstance(v, (str, int)) and v != "" for v in (repo, stage)) or not isinstance(number, int):
        return web.json_response({"error": "missing_fields", "required": ["repo", "number", "stage"]}, status=400)
    reason = body.get("reason") or ""
    edit_instruction = body.get("edit_instruction") or ""
    submitted = await asyncio.to_thread(
        approval_submit_verdict,
        repo,
        int(number),
        stage=stage,
        verdict=verdict,
        winner="dashboard",
        reason=str(reason),
        edit_instruction=str(edit_instruction),
    )
    if not submitted:
        return web.json_response(
            {"error": "already_submitted", "slug_id": slug, "repo": repo, "number": number, "stage": stage},
            status=409,
        )
    request.app["aggregator"].invalidate_cache()
    return web.json_response({"ok": True, "verdict": verdict, "slug_id": slug})


async def handle_kill(request: web.Request) -> web.Response:
    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid_json"}, status=400)
    if body.get("confirm") is not True:
        return web.json_response({"error": "confirm_required"}, status=400)
    window = body.get("window") or body.get("court_id")
    if not isinstance(window, str) or not window:
        return web.json_response({"error": "missing_window"}, status=400)
    if "/" in window or window.startswith(".") or any(c in window for c in (" ", ";", "&", "|", "$", "`")):
        return web.json_response({"error": "unsafe_window_name"}, status=400)
    target = f"{DASHBOARD_SESSION}:{window}"
    proc = await asyncio.create_subprocess_exec(
        "tmux", "kill-window", "-t", target,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return web.json_response(
            {"error": "tmux_kill_failed", "detail": stderr.decode("utf-8", errors="replace").strip()},
            status=500,
        )
    request.app["aggregator"].invalidate_cache()
    return web.json_response({"ok": True, "killed": window})


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# T-15-09: 静态文件 + 缺 dist 提示
# ---------------------------------------------------------------------------


def _add_static_routes(app: web.Application) -> None:
    async def serve_index(request: web.Request) -> web.StreamResponse:
        dist: Path = request.app["frontend_dist"]
        index = dist / "index.html"
        if index.exists():
            # PR-16a: HTML shell 自身没 hash, 必须禁缓存; 否则浏览器抓老 HTML
            # → 引用过期的 /assets/index-<hash>.js → 升级后看到老 UI (Edge 实测).
            # assets/* 路径走 hash, 仍是默认长缓存.
            return web.FileResponse(index, headers={"Cache-Control": "no-store"})
        return _render_missing_dist(dist)

    app.router.add_get("/", serve_index)

    async def serve_assets(request: web.Request) -> web.StreamResponse:
        dist: Path = request.app["frontend_dist"]
        rel = request.match_info["path"]
        candidate = (dist / "assets" / rel).resolve()
        try:
            candidate.relative_to((dist / "assets").resolve())
        except ValueError:
            raise web.HTTPNotFound()
        if not candidate.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(candidate)

    app.router.add_get("/assets/{path:.+}", serve_assets)


def _render_missing_dist(dist: Path) -> web.Response:
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>court-dashboard 未构建</title>
<style>body{{font:14px ui-sans-serif,system-ui;background:#0b0d10;color:#cdd6dd;padding:48px;}}
code,pre{{background:#1a1f25;color:#a5d4ff;padding:2px 6px;border-radius:4px;}}
pre{{padding:12px;display:block;overflow-x:auto;}}
.box{{max-width:680px;border:1px solid #2a3239;padding:24px;border-radius:8px;background:#10141a;}}
h1{{margin-top:0;color:#fff;}}</style></head>
<body><div class="box"><h1>frontend 没构建</h1>
<p>找不到 <code>{dist}/index.html</code>. 先 build:</p>
<pre>cd frontend
npm install
npm run build</pre>
<p>build 完刷新浏览器即可.</p></div></body></html>"""
    return web.Response(text=html, status=200, content_type="text/html")


# ---------------------------------------------------------------------------
# 启动入口 (复用 PR-14 receiver 套路: signal / OSError → exit 4)
# ---------------------------------------------------------------------------


async def _run_app(host: str, port: int, app: web.Application) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    _log.info(event="listening", host=host, port=port)
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


def _resolve_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token
    env_token = os.environ.get("COURT_DASHBOARD_TOKEN")
    if env_token:
        return env_token
    raise SystemExit(
        "no token provided. pass --token or set COURT_DASHBOARD_TOKEN env"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m court_dashboard_server")
    parser.add_argument("--host", default=os.environ.get("COURT_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("COURT_DASHBOARD_PORT", "9100")))
    parser.add_argument("--token", default=None, help="auth token (or COURT_DASHBOARD_TOKEN env)")
    parser.add_argument("--state-dir", default=None, help="override gitea-watcher state dir")
    parser.add_argument("--frontend-dist", default=None, help="override frontend/dist path")
    parser.add_argument("--no-fs-watcher", action="store_true", help="disable fs watcher (testing)")
    args = parser.parse_args(argv)

    try:
        token = _resolve_token(args.token)
    except SystemExit as exc:
        _log.error(event="token_resolve_failed", error=str(exc))
        return 2

    state_dir = Path(args.state_dir) if args.state_dir else None
    frontend_dist = Path(args.frontend_dist) if args.frontend_dist else None
    app = create_app(
        token=token,
        state_dir=state_dir,
        frontend_dist=frontend_dist,
        fs_watcher_enabled=not args.no_fs_watcher,
    )
    try:
        asyncio.run(_run_app(args.host, args.port, app))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        # PR-14 review C2 同款: 端口被占等 OSError 必须非零退出, 让 launchd 重启
        _log.error(event="startup_oserror", error=str(exc))
        return 4
    except Exception as exc:
        _log.exception(event="startup_failed", error=repr(exc))
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
