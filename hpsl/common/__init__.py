"""公共模块：DTO / VO / Enum / Exception / 错误码注册表。

依赖纪律（规格 §3）：common 不依赖任何业务模块（roles/engine/web），
仅被上游模块引用。本包可独立导入，无循环依赖。
"""

from .dto import ProjectCreateRequest, ProjectVO, ConfigVO
from .enums import Role, AttemptKind, UnitState, GapLevel, ACTORS
from .errors import BizError, ERROR_REGISTRY, register_error
from .result import Result, ErrorResponse, ok, fail, from_error

__all__ = [
    "ProjectCreateRequest",
    "ProjectVO",
    "ConfigVO",
    "Role",
    "AttemptKind",
    "UnitState",
    "GapLevel",
    "ACTORS",
    "BizError",
    "ERROR_REGISTRY",
    "register_error",
    "Result",
    "ErrorResponse",
    "ok",
    "fail",
    "from_error",
]
