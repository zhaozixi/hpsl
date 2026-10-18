"""密钥解析（F8.2，M4；M25 对齐《安全设计》§6 的密钥红线）。

设计（M25 收紧）：
- 三后端可插拔：MemoryBackend（测试）、KeyringBackend（OS 密钥环，可用时优先）、
  FileBackend（文件保险库，`secrets_dir/{provider}.fernet`，权限 0600）。
- **默认链路禁止明文**：`FileBackend` 只有在「口令可用且 cryptography 可用」时才工作；
  缺少口令 → 抛 A040003（fail-fast，提示设置 `HPSL_MASTER_PASSPHRASE`）；缺少 cryptography
  → 同样失败（不再静默降级）。`PlaintextCipher` 仅保留给显式注入（测试/自定义后端）。
- **每文件随机 16 字节 salt**：文件格式 `b"HPSL2" + salt(16) + Fernet token`；
  口令经 Argon2id（RFC 9106：memory 64MiB、time_cost 3、parallelism 4）派生。
- 旧格式（固定 `MASTER_SALT`、无文件头）仍可读：命中 legacy 分支时用固定盐尝试解密，
  保证升级前的密钥文件不失效。
- 口令来源：构造参数 > 环境变量 `HPSL_MASTER_PASSPHRASE`；**绝不落盘**。
- 密钥红线（D1 §6.5）：完整密钥禁止进入日志 / VO / 导出；`key_status` 仅暴露后 4 位。

纪律（冻结）：仅依赖 common.errors；不引入 ORM；不得被 common 反向依赖。
"""

from __future__ import annotations

import hashlib
import os
import warnings
from pathlib import Path
from typing import Optional, Protocol

from ..common.errors import BizError

# 仅用于旧格式（CHG 前）文件的兼容解密
MASTER_SALT = b"hpsl-static-enc-v1"
# 新文件格式：magic + salt + token
_FILE_MAGIC = b"HPSL2"
_SALT_LEN = 16

# Argon2id 参数（对齐《安全设计》§6.1：内存 ≥64MiB、迭代 ≥3、并行度 4）
ARGON2_TIME_COST = 3
ARGON2_MEMORY_KIB = 65536
ARGON2_PARALLELISM = 4


# ---------------------------------------------------------------------------
# Cipher 协议（加解密可插拔）
# ---------------------------------------------------------------------------
class Cipher(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, blob: bytes) -> bytes: ...


class PlaintextCipher:
    """明文 Cipher（**不安全**）：仅用于显式注入（测试 / 自定义后端）。

    M25 起默认链路不再自动降级到本类；构造仍发告警，提醒调用方这不是生产级加密。
    """

    def __init__(self):
        warnings.warn(
            "secrets: 使用明文密钥保险库（仅限显式注入，如测试）。"
            "生产环境请使用 FernetCipher（Argon2id 派生）或系统 keyring。",
            RuntimeWarning,
            stacklevel=2,
        )

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, blob: bytes) -> bytes:
        return blob


class FernetCipher:
    """Fernet 静态加密 Cipher（需 cryptography）。"""

    def __init__(self, key: bytes):
        from cryptography.fernet import Fernet

        self._fernet = Fernet(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        self._fernet_ok = True
        return self._fernet.encrypt(plaintext)

    def decrypt(self, blob: bytes) -> bytes:
        return self._fernet.decrypt(blob)


def derive_fernet_key(passphrase: str, *, salt: Optional[bytes] = None) -> tuple[bytes, bytes]:
    """经 Argon2id（RFC 9106）由口令派生 Fernet 密钥（需 argon2-cffi）。

    返回 (fernet_key_b64, salt)；salt 缺省时随机生成 16 字节（**每文件独立**）。
    无 argon2 时抛 RuntimeError（明确提示缺少可选依赖）。
    """
    from argon2.low_level import Type, hash_secret_raw
    from cryptography.fernet import base64

    if salt is None:
        salt = os.urandom(_SALT_LEN)
    raw = hash_secret_raw(
        passphrase.encode("utf-8"),
        salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_KIB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=32,
        type=Type.ID,
    )
    return base64.urlsafe_b64encode(raw), salt


def _env_passphrase() -> Optional[str]:
    """口令仅来自环境变量（不落盘）。"""
    return os.environ.get("HPSL_MASTER_PASSPHRASE") or None


def _legacy_passphrase(secrets_dir: str) -> str:
    """旧格式（无口令时）的确定性口令，仅用于读取 CHG 前的历史文件。"""
    return hashlib.sha256((str(secrets_dir) + ":hpsl-v1").encode("utf-8")).hexdigest()


def _locked_error() -> BizError:
    return BizError(
        "A040003",
        msg=(
            "密钥库已锁定：请设置环境变量 HPSL_MASTER_PASSPHRASE 后重启 hpsl serve"
            "（口令只驻留进程内存，不落盘）"
        ),
    )


# ---------------------------------------------------------------------------
# 后端（KeyBackend 协议）
# ---------------------------------------------------------------------------
class KeyBackend(Protocol):
    def get(self, provider: str) -> Optional[str]: ...
    def set(self, provider: str, key: str) -> None: ...
    def location(self) -> str: ...


class MemoryBackend:
    """内存后端（测试 / 注入用，无文件系统、无加密）。"""

    def __init__(self, store: Optional[dict] = None):
        self._store: dict[str, str] = dict(store or {})

    def get(self, provider: str) -> Optional[str]:
        return self._store.get(provider)

    def set(self, provider: str, key: str) -> None:
        self._store[provider] = key

    def location(self) -> str:
        return "memory"


class FileBackend:
    """文件保险库后端：`secrets_dir/{provider}.fernet`，权限 0600。

    - 显式注入 `cipher=` 时按注入的 Cipher 原样读写（无文件头，兼容既有测试）；
    - 否则要求口令可用：`HPSL2 + 每文件随机 salt + Fernet token`。
    """

    def __init__(
        self,
        secrets_dir: str | Path,
        *,
        cipher: Optional[Cipher] = None,
        passphrase: Optional[str] = None,
    ):
        self.secrets_dir = Path(secrets_dir)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self._injected = cipher
        if cipher is not None:
            self.cipher: Cipher = cipher  # 兼容既有属性访问
        self._passphrase = passphrase if passphrase is not None else _env_passphrase()

    # -- 内部：加解密 -----------------------------------------------------
    def _encrypt(self, data: bytes) -> bytes:
        if self._injected is not None:
            return self._injected.encrypt(data)
        if not self._passphrase:
            raise _locked_error()
        salt = os.urandom(_SALT_LEN)
        cipher = FernetCipher(derive_fernet_key(self._passphrase, salt=salt)[0])
        return _FILE_MAGIC + salt + cipher.encrypt(data)

    def _decrypt(self, blob: bytes) -> bytes:
        if self._injected is not None:
            return self._injected.decrypt(blob)
        if not self._passphrase:
            raise _locked_error()
        if blob.startswith(_FILE_MAGIC):
            start = len(_FILE_MAGIC)
            salt = blob[start : start + _SALT_LEN]
            token = blob[start + _SALT_LEN :]
            return FernetCipher(derive_fernet_key(self._passphrase, salt=salt)[0]).decrypt(token)
        # legacy：固定 salt + （环境口令或旧的确定性口令）
        legacy_pw = self._passphrase or _legacy_passphrase(str(self.secrets_dir))
        return FernetCipher(derive_fernet_key(legacy_pw, salt=MASTER_SALT)[0]).decrypt(blob)

    def _path(self, provider: str) -> Path:
        # provider 仅作文件名片段，需安全化避免路径穿越
        safe = "".join(c for c in provider if c.isalnum() or c in "-_")
        return self.secrets_dir / f"{safe}.fernet"

    # -- KeyBackend 协议 --------------------------------------------------
    def get(self, provider: str) -> Optional[str]:
        p = self._path(provider)
        if not p.exists():
            return None
        try:
            return self._decrypt(p.read_bytes()).decode("utf-8")
        except BizError:
            raise
        except Exception:
            return None

    def set(self, provider: str, key: str) -> None:
        p = self._path(provider)
        p.write_bytes(self._encrypt(key.encode("utf-8")))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass  # 某些平台 chmod 不可达，不阻断

    def location(self) -> str:
        return "fernet_file"


def _try_keyring_backend() -> Optional[KeyBackend]:
    try:
        import keyring
    except ImportError:
        return None

    class _Keyring:
        def get(self, provider: str) -> Optional[str]:
            try:
                return keyring.get_password("hpsl", provider)
            except Exception:
                return None

        def set(self, provider: str, key: str) -> None:
            keyring.set_password("hpsl", provider, key)

        def location(self) -> str:
            return "keyring"

    return _Keyring()


def _default_secrets_dir() -> Path:
    """默认密钥目录：`HPSL_HOME/secrets`（与 routes/settings_keys 同一位置）。

    历史实现曾返回本包目录（`hpsl/secrets/`），导致"设置页存进去、适配器读不到"；
    M28 起统一为 HPSL_HOME/secrets，按调用时解析（测试可逐用例改 HPSL_HOME）。
    """
    from .. import config as _config

    return _config.home() / "secrets"


def _default_backends(secrets_dir: Optional[str | Path]) -> list[KeyBackend]:
    backs: list[KeyBackend] = []
    kr = _try_keyring_backend()
    if kr is not None:
        backs.append(kr)
    backs.append(FileBackend(secrets_dir or _default_secrets_dir()))
    return backs


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def lock_state(secrets_dir: Optional[str | Path] = None) -> dict:
    """密钥库是否可用：`{"locked", "stored_in", "reason"}`。

    - keyring 可用 → 不锁定；
    - 否则必须有 HPSL_MASTER_PASSPHRASE，否则文件保险库无法加解密（fail-fast）。
    """
    if _try_keyring_backend() is not None:
        return {"locked": False, "stored_in": "keyring", "reason": None}
    if _env_passphrase():
        return {"locked": False, "stored_in": "fernet_file", "reason": None}
    return {
        "locked": True,
        "stored_in": None,
        "reason": (
            "未检测到系统密钥环，且未设置 HPSL_MASTER_PASSPHRASE；"
            "文件保险库无法加密/解密密钥"
        ),
    }


def get_api_key(
    provider: str,
    *,
    backend: Optional[KeyBackend] = None,
    secrets_dir: Optional[str | Path] = None,
) -> str:
    """取回 provider 密钥。所有后端均无 → A040003；密钥库锁定 → A040003（带操作提示）。"""
    if backend is not None:
        val = backend.get(provider)
        if not val:
            raise BizError("A040003", msg=f"Provider {provider} 密钥缺失或无效")
        return val
    for b in _default_backends(secrets_dir):
        val = b.get(provider)
        if val:
            return val
    raise BizError("A040003", msg=f"Provider {provider} 密钥缺失或无效")


def set_api_key(
    provider: str,
    key: str,
    *,
    backend: Optional[KeyBackend] = None,
    secrets_dir: Optional[str | Path] = None,
) -> None:
    """写入 provider 密钥（优先 keyring，否则文件保险库；锁定 → A040003）。"""
    if backend is not None:
        backend.set(provider, key)
        return
    kr = _try_keyring_backend()
    if kr is not None:
        kr.set(provider, key)
        return
    FileBackend(secrets_dir or _default_secrets_dir()).set(provider, key)


def key_status(
    provider: str,
    *,
    backend: Optional[KeyBackend] = None,
    secrets_dir: Optional[str | Path] = None,
) -> dict:
    """密钥状态：**仅**暴露 has_key / stored_in / key_tail（后 4 位）。

    完整密钥绝不返回、绝不进入任何 VO / 日志 / 导出（D1 §6.5）。
    密钥库锁定（A040003）时按"无密钥"处理，由 `lock_state()` 单独暴露锁定原因。
    """
    if backend is not None:
        try:
            val = backend.get(provider)
        except BizError:
            val = None
        return {
            "provider": provider,
            "has_key": bool(val),
            "stored_in": backend.location() if val else None,
            "key_tail": (val[-4:] if val else None),
        }
    stored_in: Optional[str] = None
    key_tail: Optional[str] = None
    has = False
    for b in _default_backends(secrets_dir):
        try:
            val = b.get(provider)
        except BizError:
            continue
        if val:
            has = True
            stored_in = b.location()
            key_tail = val[-4:]
            break
    return {"provider": provider, "has_key": has, "stored_in": stored_in, "key_tail": key_tail}
