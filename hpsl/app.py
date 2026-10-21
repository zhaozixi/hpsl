"""FastAPI 入口（M1 Web 接入 + M10 治理度量 / 健康 / 优雅停机）。

- `app`：FastAPI 应用，挂载路由（API + 页面）、安全中间件、BizError 处理器。
- `serve()`：以 uvicorn 单 worker 监听 127.0.0.1:<port>（端口可配，默认 8765）；SIGTERM/SIGINT
  触发优雅停机（停止接入新流量 → 处理在飞请求 ≤30s → 释放连接隐含 WAL checkpoint）。
- `main()`：命令行入口 `hpsl serve [...]`（标准库 argparse）。

运行形态（冻结 §2.3）：本地单机、pipx 安装、`hpsl serve` 仅监听回环地址。
所有写操作经 db.write 唯一入口 + 安全依赖（Origin/CSRF/限流）；读接口仅 Host 白名单 + 限流。
"""

from __future__ import annotations

import argparse
import getpass
import html as _html
import os
import sys
from pathlib import Path
from typing import Callable

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # M0 阶段允许在无 fastapi 环境下导入本模块（仅占位）
    FastAPI = None  # type: ignore

from . import config as _config
from .common.result import fail, from_error
from .common.errors import BizError
from .routes import security, projects_routes
from .routes import roles as roles_routes
from .routes import governance as governance_routes
from .routes import ledger as ledger_routes
from .routes import review as review_routes
from .routes import unit as unit_routes
from .routes import backup as backup_routes
from .routes import graph as graph_routes
from .routes import quota as quota_routes
from .routes import settings_keys as settings_keys_routes
from .routes import wizard as wizard_routes
from .routes import export as export_routes
from .routes import stats as stats_routes
from .routes import ops as ops_routes
from .routes import judge as judge_routes
from .routes import providers as providers_routes
from .routes import retrieval as retrieval_routes
from .routes import multi as multi_routes
from .routes import insight as insight_routes

if FastAPI is not None:
    # 生产形态关闭交互文档与 OpenAPI（安全设计 §4.2.2 / T-15）；无对外 API 消费方
    app = FastAPI(
        title="hpsl",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # 中间件顺序（Starlette 语义：最后添加者最外层）：安全中间件在内，traceId 中间件最外层包裹。
    # traceId 最外层 → 请求最先经它生成/透传 trace_id，安全层拒绝（如 Host 非白名单）的响应也会回写
    # X-Trace-Id，保证拒绝路径同样可观测（贯穿全程）。
    app.add_middleware(security.SecurityMiddleware)
    app.add_middleware(security.TraceIdMiddleware)

    @app.middleware("http")
    async def _inject_active_ctx(request: Request, call_next):
        """把「当前项目」塞进 request.state，供 base.html 顶栏常驻提示使用。

        只读进程内缓存（set_active 时写入），**不额外查库**；值为 None 时模板不出横幅。
        这也兑现「可见」原则：active 是隐式全局上下文（22 个文件 34 处依赖它），
        让用户在任何页面都能一眼看到自己正在哪个项目里操作。
        """
        from . import projects as _projects

        try:
            request.state.active_label = _projects.active_label()
        except Exception:  # 顶栏提示失败绝不能影响正常请求
            request.state.active_label = None
        return await call_next(request)

    # 挂载路由：API（/api/v1）+ 页面（Jinja2 + HTMX）
    app.include_router(projects_routes.api_router)
    app.include_router(roles_routes.api_router)
    app.include_router(governance_routes.api_router)
    app.include_router(review_routes.api_router)
    app.include_router(unit_routes.api_router)
    app.include_router(backup_routes.api_router)
    app.include_router(graph_routes.api_router)
    app.include_router(quota_routes.api_router)
    app.include_router(settings_keys_routes.api_router)
    app.include_router(export_routes.api_router)
    app.include_router(stats_routes.api_router)
    app.include_router(ops_routes.api_router)
    app.include_router(judge_routes.api_router)
    app.include_router(providers_routes.api_router)
    app.include_router(retrieval_routes.api_router)
    app.include_router(multi_routes.api_router)
    app.include_router(insight_routes.api_router)
    app.include_router(projects_routes.page_router)
    app.include_router(ledger_routes.page_router)
    app.include_router(review_routes.page_router)
    app.include_router(unit_routes.page_router)
    app.include_router(backup_routes.page_router)
    app.include_router(graph_routes.page_router)
    app.include_router(quota_routes.page_router)
    app.include_router(wizard_routes.page_router)
    app.include_router(export_routes.page_router)
    app.include_router(stats_routes.page_router)
    app.include_router(insight_routes.page_router)
    app.include_router(retrieval_routes.page_router)
    app.include_router(settings_keys_routes.page_router)

    # 静态资源（HTMX / app.js / app.css）；目录不存在时跳过挂载，避免无前端时启动失败
    _static_dir = Path(__file__).resolve().parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.exception_handler(BizError)
    def _biz_error_handler(request: Request, exc: BizError):
        """业务/系统/客户端错误按调用方分流。

        - 普通 REST：回显 Result.to_dict()，HTTP 状态取 exc.http_status，携带 traceId。
        - HTMX 片段请求：返回人读 HTML 片段。**状态码固定 200**——HTMX 默认只 swap 2xx，
          若沿用 4xx/5xx 会导致错误被静默丢弃，用户点了没反应。错误码仍写进文案，便于排查。
        """
        tid = getattr(request.state, "trace_id", None)
        if request.headers.get("hx-request") is not None:
            body = f'<p class="result err">{_html.escape(exc.msg)}（{exc.code}）</p>'
            # 上游/底层原文只放进折叠区：默认收起，不干扰「该怎么办」，要排障时再展开。
            detail = getattr(exc, "detail", None)
            if detail:
                body += (
                    '<details><summary class="text-muted">技术细节</summary>'
                    f"<pre>{_html.escape(str(detail))}</pre></details>"
                )
            return HTMLResponse(content=body, status_code=200)
        return JSONResponse(status_code=exc.http_status, content=from_error(exc, trace_id=tid).to_dict())

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        """未捕获异常统一收口：日志留栈、对外只给 B999001，避免裸 500/栈泄漏。"""
        import logging

        logging.getLogger("hpsl").exception("未处理异常：%s %s", request.method, request.url.path)
        tid = getattr(request.state, "trace_id", None)
        if request.headers.get("hx-request") is not None:
            return HTMLResponse(
                content='<p class="result err">系统内部错误，请稍后再试（B999001）</p>',
                status_code=200,
            )
        return JSONResponse(status_code=500, content=fail("B999001", trace_id=tid).to_dict())

    @app.get("/healthz")
    def healthz():
        # Liveness：进程存活（§3.2.M10.2）
        return {"status": "ok", "service": "hpsl", "phase": "M40"}

    @app.get("/readyz")
    def readyz():
        # Readiness：项目库目录可读写（§3.2.M10.2）；不写探针文件，避免每次探测留垃圾
        try:
            root = _config.projects_root()
            ready = root.exists() and _writable(root)
        except Exception:
            ready = False
        if not ready:
            return JSONResponse(status_code=503, content={"status": "not_ready", "service": "hpsl"})
        return {"status": "ok", "service": "hpsl", "phase": "M40"}

else:  # pragma: no cover - 占位降级
    app = None


def _writable(path: Path) -> bool:
    """目录可写性检查（不落探针文件）。"""
    import os

    return os.access(path, os.W_OK)


def _ensure_passphrase_interactive(
    *,
    isatty: Callable[[], bool] | None = None,
    getpass_fn: Callable[[str], str] | None = None,
    lock_state_fn: Callable[[], dict] | None = None,
) -> bool:
    """P2-08：启动时按需交互式询问密钥库口令（`hpsl serve` 专用）。

    行为：
    - 仅当 `keystore.lock_state()["locked"]` 为真才介入（keyring 可用 / 已设口令 → 直接返回）；
    - `HPSL_NO_PASSPHRASE_PROMPT=1` → 完全静默跳过（供 systemd / 无头运维显式关闭）；
    - 标准输入是 TTY → 用 `getpass` 询问，非空口令写入 `HPSL_MASTER_PASSPHRASE`
      （只驻留进程内存、绝不落盘）；空输入 / EOF / Ctrl-C 视为跳过；
    - 非交互（管道 / 守护进程 / 重定向）→ 不询问，向 stderr 打印一行提示。

    返回是否成功设置了口令。`isatty` / `getpass_fn` / `lock_state_fn` 可注入便于单测
    （默认分别为 `sys.stdin.isatty` / `getpass.getpass` / `keystore.lock_state`）。
    """
    from .secrets import keystore

    state = (lock_state_fn or keystore.lock_state)()
    if not state.get("locked"):
        return False

    # 显式关闭提示：无头部署不应被 stdin 阻塞，也不应刷屏
    if os.environ.get("HPSL_NO_PASSPHRASE_PROMPT") == "1":
        return False

    try:
        interactive = bool((isatty or sys.stdin.isatty)())
    except Exception:  # stdin 已关闭 / 非标准流：按非交互处理
        interactive = False

    if interactive:
        try:
            passphrase = (getpass_fn or getpass.getpass)(
                "HPSL_MASTER_PASSPHRASE（密钥库口令，回车跳过）: "
            )
        except (EOFError, KeyboardInterrupt):
            passphrase = ""
        if passphrase:
            os.environ["HPSL_MASTER_PASSPHRASE"] = passphrase
            return True

    print(
        "[hpsl] 密钥库未解锁（未检测到系统密钥环且未设口令）："
        "本次会话将无法保存/读取密钥；请设置 HPSL_MASTER_PASSPHRASE 后重启，"
        "或配置系统密钥环。",
        file=sys.stderr,
    )
    return False


def _restore_disabled() -> bool:
    """是否禁用启动恢复：env `HPSL_NO_RESTORE_ACTIVE` 或 CLI `--no-restore`。

    「可关」原则的落点：恢复是便捷性优化，若用户想要确定性的「每次从空态开始」
    （或脚本化启动、多实例共用同一 HPSL_HOME），应能一键关掉。
    """
    val = os.environ.get("HPSL_NO_RESTORE_ACTIVE", "")
    return val.strip().lower() in ("1", "true", "yes", "on")


def _restore_active_project() -> None:
    """启动时恢复上次所在项目（best-effort，失败不影响启动）。

    显式调用点只在 serve()：**绝不放模块导入期**——否则测试与其它进程会被外部
    数据目录的 last_active_at 左右，破坏隔离性。
    """
    import logging

    if _restore_disabled():
        print("[serve] 已按 HPSL_NO_RESTORE_ACTIVE 跳过恢复上次项目")
        return
    from . import projects as _projects

    try:
        vo = _projects.restore_last_active()
    except Exception:  # 单项目损坏等：降级为空态，绝不阻断启动
        logging.getLogger("hpsl").warning("恢复上次项目失败，本次以「未选择项目」启动", exc_info=True)
        return
    if vo is None:
        print("[serve] 无历史活跃项目记录，以「未选择项目」启动")
    else:
        print(f"[serve] 已恢复到上次项目：{vo.name}（{vo.id}）")


def _prepare_serve(host: str, port: int | None) -> tuple[str, int]:
    """启动前归一：拒绝非回环监听，并把实际端口写回 HPSL_PORT。

    M36/A4：此前 `serve --port 9000` 只改 uvicorn、不改 `HPSL_PORT`，
    Host/Origin 白名单仍按 8765 计算，请求全部 403。这里显式对齐。
    """
    loopback = {"127.0.0.1", "localhost", "::1"}
    if host not in loopback:
        allow = os.environ.get("HPSL_ALLOW_NON_LOOPBACK", "").strip().lower()
        if allow not in ("1", "true", "yes", "on"):
            raise SystemExit(
                "[serve] HPSL 仅支持回环地址（127.0.0.1 / localhost / ::1）；"
                "确需外网监听请显式设置 HPSL_ALLOW_NON_LOOPBACK=1 并自行承担风险"
            )
    real_port = int(port) if port is not None else _config.service_port()
    os.environ["HPSL_PORT"] = str(real_port)
    return host, real_port


def serve(host: str = "127.0.0.1", port: int | None = None, restore: bool = True) -> None:
    """启动 uvicorn 单 worker（监听回环地址）；优雅停机（SIGTERM/SIGINT 由 uvicorn 处理）。"""
    if app is None:
        raise RuntimeError("FastAPI 未安装，无法启动 serve；请先 pip install fastapi uvicorn")
    host, port = _prepare_serve(host, port)
    # P2-08：启动口令（交互式）。必须早于任何 db 操作，避免锁定态下做出误导性读写。
    _ensure_passphrase_interactive()

    import atexit
    import uvicorn

    from . import db

    # 退出前释放全部项目连接（隐含 WAL checkpoint），保证本地库持久化
    atexit.register(db.close_all)

    # 恢复上次所在项目（在库可读之后、开始服务之前；best-effort）
    if restore:
        _restore_active_project()
    else:
        print("[serve] 已按 --no-restore 跳过恢复上次项目")

    # 启动即维护：补当日快照 + 清理各项目过期快照（零 DDL，best-effort，避免阻塞启动）
    try:
        db.daily_snapshot_all()
    except Exception:
        pass
    try:
        db.rotate_all_backups()
    except Exception:
        pass

    # 周期定时维护（本地单用户工具的「调度」实现）：daemon 线程，间隔经 env 可配
    import logging
    import os
    import threading

    _interval = int(os.environ.get("HPSL_ROTATE_INTERVAL_SECONDS", "21600"))  # 默认 6h
    _stop = threading.Event()

    def _maintenance_loop() -> None:
        while not _stop.is_set():
            if _stop.wait(_interval):
                break
            try:
                db.daily_snapshot_all()
            except Exception:  # 单轮失败不影响主服务
                logging.getLogger("hpsl").warning("周期每日快照失败", exc_info=True)
            try:
                db.rotate_all_backups()
            except Exception:  # 单轮失败不影响主服务
                logging.getLogger("hpsl").warning("周期备份轮转失败", exc_info=True)

    _maintenance_thread = threading.Thread(
        target=_maintenance_loop, name="hpsl-backup-maintenance", daemon=True
    )
    _maintenance_thread.start()
    atexit.register(_stop.set)

    port = port or _config.service_port()
    cfg = uvicorn.Config(app, host=host, port=port, workers=1, timeout_graceful_shutdown=30)
    server = uvicorn.Server(cfg)
    server.run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hpsl", description="HPSL 项目制学习框架（M10 Web 接入）")
    sub = parser.add_subparsers(dest="cmd")
    sub_serve = sub.add_parser("serve", help="启动本地服务（127.0.0.1:<port>）")
    sub_serve.add_argument("--host", default="127.0.0.1")
    sub_serve.add_argument("--port", type=int, default=None)
    sub_serve.add_argument(
        "--no-restore",
        action="store_true",
        help="启动时不恢复上次所在项目（每次从「未选择项目」开始）",
    )
    sub_rotate = sub.add_parser("rotate", help="立即轮转全部/指定项目的过期快照（可外部 cron）")
    sub_rotate.add_argument("--pid", default=None, help="指定项目 id；缺省则全部项目")
    sub_backup = sub.add_parser("backup", help="立即导出并轮转全部/指定项目快照（可外部 cron）")
    sub_backup.add_argument("--pid", default=None, help="指定项目 id；缺省则全部项目")
    sub_snapshot = sub.add_parser("snapshot", help="立即为全部项目补当天（UTC）快照（幂等，可外部 cron）")
    sub_restore = sub.add_parser("restore", help="从项目 backups/ 内快照恢复 project.db")
    sub_restore.add_argument("--pid", required=True, help="项目 id")
    sub_restore.add_argument("--file", required=True, help="备份文件（文件名或 backups/ 内路径）")
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(host=args.host, port=args.port, restore=not args.no_restore)
    elif args.cmd == "rotate":
        from . import db
        if getattr(args, "pid", None):
            deleted = db.rotate_backups(args.pid)
            print(f"[rotate] {args.pid}: deleted {len(deleted)} -> {deleted}")
        else:
            res = db.rotate_all_backups()
            for pid, dl in res.items():
                print(f"[rotate] {pid}: deleted {len(dl)} -> {dl}")
            if not res:
                print("[rotate] nothing to clean")
    elif args.cmd == "backup":
        from . import db
        from . import projects as _projects
        pids = [args.pid] if getattr(args, "pid", None) else [p.id for p in _projects.list_projects()]
        for pid in pids:
            try:
                path = db.backup_project(pid)
                print(f"[backup] {pid}: {path}")
            except Exception as e:  # 单项目失败不中断其余
                print(f"[backup] {pid} failed: {e}")
    elif args.cmd == "snapshot":
        from . import db
        created = db.daily_snapshot_all()
        for pid, path in created.items():
            print(f"[snapshot] {pid}: {path}")
        if not created:
            print("[snapshot] up to date")
    elif args.cmd == "restore":
        from . import db
        try:
            info = db.restore_project(args.pid, args.file)
        except BizError as e:  # 业务/参数错误：打印错误并以退出码 1 结束，不抛未捕获堆栈
            print(f"[restore] {args.pid} failed: [{e.code}] {e.msg}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:  # 文件/数据库异常同样收敛为退出码 1
            print(f"[restore] {args.pid} failed: {e}", file=sys.stderr)
            sys.exit(1)
        print(
            f"[restore] {args.pid}: restored_from={info['restored_from']} "
            f"pre_restore={info['pre_restore']} objects={info['objects']}"
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
