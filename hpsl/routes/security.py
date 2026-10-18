"""安全基线（冻结项 #5，本地等效实现）：Host 白名单 + 写请求 Origin/CSRF 双提交 + safe_path + 五维限流。

- Host 白名单：中间件校验 `Host` 头，非白名单 → 400 C400003（规格 §5）。
- Origin 校验（写）：解析 scheme://netloc 须为回环，否则 → 403 C403001。
- CSRF 双提交：GET 经中间件下发可读 cookie；写请求须带 `X-CSRF-Token` 且等于 cookie，否则 → 403 C403001。
- safe_path：路由层凡涉及 pid/路径访问先解析并校验前缀，越界 → C400002（复用 M0 db.safe_path）。
- 五维限流：进程内滑动窗口（进程/路由/LLM频次/SSE并发/写频次），越限 → 429 C429001。

错误码说明：M1 §2.1 提及 A020001/A020002/A020003，但基线 §3.5.1 已将这些 A02xx 码分配给 M2 状态机，
且 C403001 已涵盖 Host/Origin/CSRF 来源校验。为不破坏锁定基线，本实现沿用 C 类客户端错误码：
C400003（Host 不在白名单）、C403001（来源不被允许）、C429001（限流）。该冲突已上报主理人裁决。
"""

from __future__ import annotations

import threading
import time
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import config
from ..common.errors import BizError
from ..common.result import fail

# 常量
CSRF_COOKIE = "hpsl_csrf"
X_CSRF_HEADER = "x-csrf-token"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.1"}


# ---------------------------------------------------------------------------
# Host 白名单（冻结项 #5）
# ---------------------------------------------------------------------------
def check_host(host: str) -> bool:
    als = {h.lower() for h in config.allowed_hosts()}
    if not als:
        return True  # 空白名单视为全部允许（不应发生，config 默认非空）
    host = (host or "").split(":")[0].lower()
    return host in als or host.startswith("127.")


# ---------------------------------------------------------------------------
# Origin 校验（写请求，回环）
# ---------------------------------------------------------------------------
def check_origin(origin: str) -> bool:
    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname or ""
    host = host.lower()
    if host in _LOOPBACK_HOSTS or host.startswith("127.") or host == "localhost":
        return True
    return False


# ---------------------------------------------------------------------------
# CSRF 双提交
# ---------------------------------------------------------------------------
def new_csrf_token() -> str:
    return secrets.token_hex(32)


def new_trace_id() -> str:
    """生成 32 位 hex 链路标识（M10 §3.2.M10.5 Step1）。"""
    return secrets.token_hex(16)


def check_csrf(request) -> bool:
    token = request.headers.get(X_CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    return bool(token) and bool(cookie) and token == cookie


# ---------------------------------------------------------------------------
# 五维限流：进程内滑动窗口
# ---------------------------------------------------------------------------
class SlidingWindowLimiter:
    """滑动窗口限流器：allow(dimension, key) 在窗口内计数，越限返回 False。"""

    def __init__(self, limits: dict, window: float = 1.0):
        self.limits = dict(limits)
        self.window = window
        self._lock = threading.Lock()
        self._buckets: dict[str, list[float]] = {}

    def configure(self, limits: dict, window: float | None = None) -> None:
        with self._lock:
            self.limits = dict(limits)
            if window is not None:
                self.window = window
            self._buckets.clear()

    def allow(self, dimension: str, key: str, now: float | None = None) -> bool:
        limit = self.limits.get(dimension, 10**9)
        if limit <= 0:
            return False
        now = time.monotonic() if now is None else now
        bk = f"{dimension}:{key}"
        with self._lock:
            times = self._buckets.setdefault(bk, [])
            cutoff = now - self.window
            i = 0
            while i < len(times) and times[i] <= cutoff:
                i += 1
            if i:
                del times[:i]
            if len(times) >= limit:
                return False
            times.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# 单例（进程内）
rate_limiter = SlidingWindowLimiter(config.rate_limits(), config.RATE_LIMIT_WINDOW)


# ---------------------------------------------------------------------------
# 中间件：traceId 生成与透传（M10 §3.2.M10.5 Step1）
# ---------------------------------------------------------------------------
class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        tid = request.headers.get("x-trace-id") or new_trace_id()
        request.state.trace_id = tid
        response = await call_next(request)
        # 响应头携带 traceId（错误体与日志可据此定位）
        response.headers["X-Trace-Id"] = tid
        return response


# ---------------------------------------------------------------------------
# 中间件：Host 白名单 + CSRF cookie 下发
# ---------------------------------------------------------------------------
class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Host 白名单（所有请求）
        if not check_host(request.headers.get("host", "")):
            return JSONResponse(status_code=400, content=fail("C400003").to_dict())
        response = await call_next(request)
        # CSRF cookie 下发（GET）：无则种，有则回显；httponly=False 供 JS 双提交
        if request.method == "GET":
            cookie = request.cookies.get(CSRF_COOKIE) or new_csrf_token()
            response.set_cookie(
                CSRF_COOKIE, cookie, httponly=False, samesite="lax", path="/"
            )
        return response


# ---------------------------------------------------------------------------
# FastAPI 安全依赖
# ---------------------------------------------------------------------------
def read_security(request: Request) -> None:
    """读接口：Host 已由中间件覆盖；仅限流（进程 + 路由）。"""
    _check_rate(request, write=False)


def write_security(request: Request) -> None:
    """写接口：Origin + CSRF + 限流（进程 + 路由 + 写频次）。"""
    if not check_origin(request.headers.get("origin", "")):
        raise BizError("C403001")
    if not check_csrf(request):
        raise BizError("C403001")
    _check_rate(request, write=True)


def _check_rate(request, write: bool) -> None:
    path = request.url.path
    dims = ["process", "route"]
    if write:
        dims.append("write_freq")
    for dim in dims:
        key = path if dim in ("route", "write_freq") else "global"
        if not rate_limiter.allow(dim, key):
            raise BizError("C429001")
