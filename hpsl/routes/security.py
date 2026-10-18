"""安全基线（冻结项 #5，本地等效实现）：Host 白名单 + 写请求 Origin/CSRF 双提交 + safe_path + 五维限流。

- Host 白名单：中间件校验 `Host` 头（精确匹配，含端口），非白名单 → 403 C403001（M25 起与
  《安全设计》T-01 对齐；旧 C400003 保留注册但不再使用）。
- Origin 校验（写）：解析 scheme://netloc 须为回环，否则 → 403 C403001。
- CSRF 双提交（P2-09 签名）：GET 经中间件下发可读 cookie（`nonce.sig`，HMAC 签名）；
  写请求须带 `X-CSRF-Token` 且等于 cookie，并通过签名校验，否则 → 403 C403001。
- safe_path：路由层凡涉及 pid/路径访问先解析并校验前缀，越界 → C400002（复用 M0 db.safe_path）。
- 五维限流：进程内滑动窗口（进程/路由/LLM频次/SSE并发/写频次），越限 → 429 C429001。

错误码说明：M1 期曾用 C400003（Host 400）；M25 依《安全设计》T-01 统一为 403 C403001，
C400003 仍在注册表中（向后兼容，历史客户端可识别）。
"""

from __future__ import annotations

import hashlib
import hmac
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

# ---------------------------------------------------------------------------
# Host / Origin 白名单（冻结项 #5；M25 起改为精确匹配 + fail-fast）
# ---------------------------------------------------------------------------
def _allowed_host_set() -> set[str]:
    """允许的 Host 值（含显式端口）。空白名单 → 空集（全部拒绝，不 fail-open）。"""
    port = config.service_port()
    out: set[str] = set()
    for raw in config.allowed_hosts():
        host = (raw or "").strip().lower()
        if not host:
            continue
        out.add(host)
        if ":" not in host:  # 裸主机名补「host:port」形态
            out.add(f"{host}:{port}")
    return out


def _allowed_origin_set() -> set[str]:
    """允许的 Origin 值（http/https × 带/不带端口）。"""
    port = config.service_port()
    out: set[str] = set()
    for raw in config.allowed_hosts():
        host = (raw or "").strip().lower()
        if not host:
            continue
        out.add(f"http://{host}")
        out.add(f"https://{host}")
        if ":" not in host:
            out.add(f"http://{host}:{port}")
            out.add(f"https://{host}:{port}")
    return out


def check_host(host: str) -> bool:
    """精确匹配 Host（含端口）；空白名单一律拒绝（防 DNS 重绑定）。"""
    allowed = _allowed_host_set()
    if not allowed:
        return False
    return (host or "").strip().lower() in allowed


def check_origin(origin: str) -> bool:
    """写请求 Origin 精确匹配（scheme + 主机 + 端口），不再使用 `127.` 前缀放行。"""
    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return False
    netloc = (parts.netloc or "").lower()
    if not netloc:
        return False
    return f"{parts.scheme}://{netloc}" in _allowed_origin_set()


# ---------------------------------------------------------------------------
# CSRF 双提交（HMAC 签名，P2-09）
#
# 旧实现是纯随机值双提交：攻击者若能写 cookie（子域/中间人）即可自造一对
# 匹配的 cookie/header。现升级为「签名 token」：进程启动时生成 32B 随机密钥，
# token = `nonce.signature`，服务端校验 HMAC-SHA256(secret, nonce)。
# - 密钥仅驻留进程内存（不落盘、不下发），重启即失效；
# - 重启后旧 cookie 通不过校验，GET 中间件会重新签发（见 SecurityMiddleware）；
# - 双提交仍保留：header 必须与 cookie 相同，再验签（防 cookie 注入 + 防伪造）。
# ---------------------------------------------------------------------------
secret = secrets.token_bytes(32)


def _csrf_signature(nonce: str) -> str:
    """HMAC-SHA256(secret, nonce) 的 hex 摘要（64 位小写 hex）。"""
    return hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def new_csrf_token() -> str:
    """签发 CSRF token：`nonce.sig`（nonce=16B hex，sig=32B hex）。"""
    nonce = secrets.token_hex(16)
    return f"{nonce}.{_csrf_signature(nonce)}"


def verify_csrf_token(token: str) -> bool:
    """校验签名 token：格式必须为 `nonce.sig` 且 HMAC 匹配（常量时间比较）。

    缺失 / 非字符串 / 无 `.` / 空段 / 签名不符 → False（绝不抛异常）。
    """
    if not token or not isinstance(token, str):
        return False
    nonce, sep, sig = token.partition(".")
    if not nonce or not sep or not sig:
        return False
    return hmac.compare_digest(sig, _csrf_signature(nonce))


def new_trace_id() -> str:
    """生成 32 位 hex 链路标识（M10 §3.2.M10.5 Step1）。"""
    return secrets.token_hex(16)


def check_csrf(request) -> bool:
    """写请求 CSRF：header 与 cookie 双提交一致，且 token 签名有效。"""
    token = request.headers.get(X_CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not token or not cookie or token != cookie:
        return False
    return verify_csrf_token(token)


# ---------------------------------------------------------------------------
# 五维限流：进程内滑动窗口
# ---------------------------------------------------------------------------
class SlidingWindowLimiter:
    """滑动窗口限流器：allow(dimension, key) 在窗口内计数，越限返回 False。

    窗口可按维度覆盖（`windows`），例如 LLM 频次按 60s 计，其余维度按 1s 计。
    """

    def __init__(
        self,
        limits: dict,
        window: float = 1.0,
        windows: dict | None = None,
    ):
        self.limits = dict(limits)
        self.window = window
        self.windows = dict(windows or {})
        self._lock = threading.Lock()
        self._buckets: dict[str, list[float]] = {}

    def configure(
        self,
        limits: dict,
        window: float | None = None,
        windows: dict | None = None,
    ) -> None:
        with self._lock:
            self.limits = dict(limits)
            if window is not None:
                self.window = window
            if windows is not None:
                self.windows = dict(windows)
            self._buckets.clear()

    def _window_for(self, dimension: str) -> float:
        return float(self.windows.get(dimension, self.window))

    def allow(self, dimension: str, key: str, now: float | None = None) -> bool:
        limit = self.limits.get(dimension, 10**9)
        if limit <= 0:
            return False
        window = self._window_for(dimension)
        now = time.monotonic() if now is None else now
        bk = f"{dimension}:{key}"
        with self._lock:
            times = self._buckets.setdefault(bk, [])
            cutoff = now - window
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


class ConcurrencyLimiter:
    """并发闸（SSE 用）：`try_acquire()` 成功即占用一个槽，`release()` 归还。

    与滑动窗口不同，这是"同时进行中的数量"而非"单位时间次数"。
    """

    def __init__(self, limit: int):
        self.limit = int(limit)
        self._active = 0
        self._lock = threading.Lock()

    def configure(self, limit: int) -> None:
        with self._lock:
            self.limit = int(limit)
            self._active = min(self._active, self.limit)

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def active(self) -> int:
        with self._lock:
            return self._active


# 单例（进程内）
rate_limiter = SlidingWindowLimiter(
    config.rate_limits(), config.RATE_LIMIT_WINDOW, config.rate_windows()
)
sse_limiter = ConcurrencyLimiter(int(config.rate_limits().get("sse_concurrency", 2)))


def hold_sse_slot():
    """SSE 端点依赖：占用一个并发槽，响应/断开后归还。

    当前 v0 尚无 SSE 端点（设计预留），此依赖供未来 `Depends(hold_sse_slot)` 使用；
    越限 → C429001（与五维限流同一错误码）。
    """
    if not sse_limiter.try_acquire():
        raise BizError("C429001", msg="同时进行的流式请求过多，请稍后再试")
    try:
        yield
    finally:
        sse_limiter.release()


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
        # Host 白名单（所有请求）；M25 起与文档一致返回 403/C403001
        if not check_host(request.headers.get("host", "")):
            return JSONResponse(status_code=403, content=fail("C403001").to_dict())
        response = await call_next(request)
        # CSRF cookie 下发（GET）：无则种，有则回显；httponly=False 供 JS 双提交。
        # 旧值签名无效（如进程重启后 secret 变化）→ 重新签发，避免页面停留在
        # 永远 403 的僵死 token 上。签名 token 由 `nonce.sig` 组成，仍可被 JS 读取。
        if request.method == "GET":
            cookie = request.cookies.get(CSRF_COOKIE)
            if not cookie or not verify_csrf_token(cookie):
                cookie = new_csrf_token()
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
