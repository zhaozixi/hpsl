"""统一响应与 6 位错误码封装（规格 §3.5.1 / 锁定基线 §3.2.M10.3）。

M0 为数据底座，HTTP 出口留待 M10；此处提供进程内可复用的 Result / ErrorResponse
结构与 ok()/fail() 助手，保证后续 Web 层可直接复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import BizError, error_info


@dataclass
class Result:
    """统一返回结构：code / msg / data / traceId。"""

    code: str = "0"          # 成功为 "0"，失败为 6 位错误码
    msg: str = "ok"
    data: Any = None
    traceId: Optional[str] = None

    def to_dict(self) -> dict:
        return {"code": self.code, "msg": self.msg, "data": self.data, "traceId": self.traceId}


@dataclass
class ErrorResponse:
    """错误响应体（HTTP 出口回显）。"""

    code: str
    msg: str
    traceId: Optional[str] = None
    timestamp: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "msg": self.msg,
            "traceId": self.traceId,
            "timestamp": self.timestamp,
        }


def ok(data: Any = None, trace_id: Optional[str] = None) -> Result:
    return Result(code="0", msg="ok", data=data, traceId=trace_id)


def fail(code: str, trace_id: Optional[str] = None) -> Result:
    """由 6 位错误码构造失败 Result（文案取自注册表）。"""
    info = error_info(code)
    return Result(code=code, msg=info[4], data=None, traceId=trace_id)


def from_error(err: BizError, trace_id: Optional[str] = None) -> Result:
    return Result(code=err.code, msg=err.msg, data=None, traceId=trace_id)
