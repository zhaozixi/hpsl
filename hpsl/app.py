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
from pathlib import Path

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # M0 阶段允许在无 fastapi 环境下导入本模块（仅占位）
    FastAPI = None  # type: ignore

from . import config as _config
from .common.result import fail
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

if FastAPI is not None:
    # 生产形态关闭交互文档与 OpenAPI（安全设计 §4.2.2 / T-15）；无对外 API 消费方
    app = FastAPI(
        title="hpsl",
        version="0.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # 中间件顺序（Starlette 语义：最后添加者最外层）：安全中间件在内，traceId 中间件最外层包裹。
    # traceId 最外层 → 请求最先经它生成/透传 trace_id，安全层拒绝（如 Host 非白名单）的响应也会回写
    # X-Trace-Id，保证拒绝路径同样可观测（贯穿全程）。
    app.add_middleware(security.SecurityMiddleware)
    app.add_middleware(security.TraceIdMiddleware)

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

    # 静态资源（HTMX / app.js / app.css）；目录不存在时跳过挂载，避免无前端时启动失败
    _static_dir = Path(__file__).resolve().parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.exception_handler(BizError)
    def _biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
        """业务/系统/客户端错误统一回显为 Result.to_dict()，HTTP 状态取 exc.http_status；携带 traceId。"""
        tid = getattr(request.state, "trace_id", None)
        return JSONResponse(status_code=exc.http_status, content=fail(exc.code, trace_id=tid).to_dict())

    @app.get("/healthz")
    def healthz():
        # Liveness：进程存活（§3.2.M10.2）
        return {"status": "ok", "service": "hpsl", "phase": "M10"}

    @app.get("/readyz")
    def readyz():
        # Readiness：项目库目录可读写（§3.2.M10.2）
        try:
            root = _config.projects_root()
            ready = root.exists() and _writable(root)
        except Exception:
            ready = False
        if not ready:
            return JSONResponse(status_code=503, content={"status": "not_ready", "service": "hpsl"})
        return {"status": "ok", "service": "hpsl", "phase": "M10"}

else:  # pragma: no cover - 占位降级
    app = None


def _writable(path: Path) -> bool:
    import tempfile
    try:
        tmp = path / ".hpsl-readyz-probe"
        tmp.write_text("1")
        tmp.unlink()
        return True
    except OSError:
        return False


def serve(host: str = "127.0.0.1", port: int | None = None) -> None:
    """启动 uvicorn 单 worker（监听回环地址）；优雅停机（SIGTERM/SIGINT 由 uvicorn 处理）。"""
    if app is None:
        raise RuntimeError("FastAPI 未安装，无法启动 serve；请先 pip install fastapi uvicorn")
    import atexit
    import uvicorn

    from . import db

    # 退出前释放全部项目连接（隐含 WAL checkpoint），保证本地库持久化
    atexit.register(db.close_all)

    # 启动即轮转：清理各项目过期快照（零 DDL，best-effort，避免阻塞启动）
    try:
        db.rotate_all_backups()
    except Exception:
        pass

    # 周期定时轮转（本地单用户工具的「调度」实现）：daemon 线程，间隔经 env 可配
    import logging
    import os
    import threading
    import time

    _interval = int(os.environ.get("HPSL_ROTATE_INTERVAL_SECONDS", "21600"))  # 默认 6h
    _stop = threading.Event()

    def _rotate_loop() -> None:
        while not _stop.is_set():
            if _stop.wait(_interval):
                break
            try:
                db.rotate_all_backups()
            except Exception:  # 单轮失败不影响主服务
                logging.getLogger("hpsl").warning("周期备份轮转失败", exc_info=True)

    _rotate_thread = threading.Thread(
        target=_rotate_loop, name="hpsl-backup-rotate", daemon=True
    )
    _rotate_thread.start()
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
    sub_rotate = sub.add_parser("rotate", help="立即轮转全部/指定项目的过期快照（可外部 cron）")
    sub_rotate.add_argument("--pid", default=None, help="指定项目 id；缺省则全部项目")
    sub_backup = sub.add_parser("backup", help="立即导出并轮转全部/指定项目快照（可外部 cron）")
    sub_backup.add_argument("--pid", default=None, help="指定项目 id；缺省则全部项目")
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(host=args.host, port=args.port)
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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
