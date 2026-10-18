"""密钥解析（F8.2，M4）。

设计原则（对应基线 §3.2.M4 / §7.2.3 与 M4 实现规格 §4.2）：
- 三后端可插拔，便于 headless 测试与可选依赖降级：
  - MemoryBackend：内存字典（测试 / 进程内注入，无文件系统、无加密）。
  - KeyringBackend：OS 密钥环（keyring 可用时优先）。
  - FileBackend：文件保险库，路径 `secrets_dir/{provider}.fernet`，权限 0600；
    加解密经可插拔 Cipher。默认 Cipher 为 FernetCipher（口令经 Argon2id + 固定
    MASTER_SALT 派生）；仅当 cryptography 缺失时降级 PlaintextCipher（发告警）。
    用户可经环境变量 HPSL_MASTER_PASSPHRASE 注入自设口令（最强）。
- 密钥红线（D1 §6.5）：完整密钥**禁止**进入日志 / VO / 导出；`key_status` 仅暴露后 4 位。
- A040003：所有后端均无密钥 → 抛「密钥缺失或无效」。

为何可插拔：运行环境未安装 cryptography / keyring / argon2，真实 Fernet+Argon2id 路径
经 import 守卫保留实现，但默认降级为明文 0600 文件（带告警）。测试经注入 MemoryBackend
或 FileBackend(NullCipher) 完全覆盖行为，无需任何三方库。

纪律（冻结）：仅依赖 common.errors；不引入 ORM；不得被 common 反向依赖。
"""

from __future__ import annotations

import hashlib
import os
import warnings
from pathlib import Path
from typing import Optional, Protocol

from ..common.errors import BizError


# 固定盐（非随机、不落盘）：所有单机实例默认 Fernet 派生的盐均相同，
# 以保证同机同 secrets_dir 下静态加密文件可确定性复现（重装/重启后仍能解密）。
MASTER_SALT = b"hpsl-static-enc-v1"


# ---------------------------------------------------------------------------
# Cipher 协议（加解密可插拔）
# ---------------------------------------------------------------------------
class Cipher(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, blob: bytes) -> bytes: ...


class PlaintextCipher:
    """明文 Cipher（**不安全降级**）：仅做字节透传。

    用于运行环境缺少 cryptography 时的文件保险库降级；生产必须替换为 FernetCipher。
    构造即发一次 RuntimeWarning，提醒这不是生产级加密。
    """

    def __init__(self):
        warnings.warn(
            "secrets: 使用明文密钥保险库降级（cryptography 缺失）。"
            "生产环境请注入 FernetCipher（经 Argon2id 派生的密钥）以获得静态加密。",
            RuntimeWarning,
            stacklevel=2,
        )

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, blob: bytes) -> bytes:
        return blob


class FernetCipher:
    """Fernet 静态加密 Cipher（需 cryptography）。

    key：32 字节 URL-safe base64 编码密钥（由 derive_fernet_key 经 Argon2id 派生）。
    """

    def __init__(self, key: bytes):
        from cryptography.fernet import Fernet

        self._fernet = Fernet(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, blob: bytes) -> bytes:
        return self._fernet.decrypt(blob)


def derive_fernet_key(passphrase: str, *, salt: Optional[bytes] = None) -> tuple[bytes, bytes]:
    """经 Argon2id（RFC 9106）由口令派生 Fernet 密钥（需 argon2-cffi）。

    返回 (fernet_key_b64, salt)。salt 不落盘；口令仅在内存派生。
    无 argon2 时抛 RuntimeError（明确提示缺少可选依赖）。
    """
    from cryptography.fernet import base64
    from argon2.low_level import Type, hash_secret_raw

    salt = salt or os.urandom(16)
    raw = hash_secret_raw(
        passphrase.encode("utf-8"),
        salt,
        time_cost=2,
        memory_cost=102400,
        parallelism=8,
        hash_len=32,
        type=Type.ID,
    )
    return base64.urlsafe_b64encode(raw), salt


def _default_passphrase(secrets_dir: str) -> str:
    """默认静态加密口令来源（确定性、不落盘）。

    - 环境变量 HPSL_MASTER_PASSPHRASE 已设置 → 优先使用（用户自设，最强）；
    - 否则确定性派生：基于 secrets_dir 的 sha256（同机同目录稳定可复现）。
    """
    env_pw = os.environ.get("HPSL_MASTER_PASSPHRASE")
    if env_pw:
        return env_pw
    return hashlib.sha256((str(secrets_dir) + ":hpsl-v1").encode("utf-8")).hexdigest()


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
    """文件保险库后端：secrets_dir/{provider}.fernet，权限 0600。"""

    def __init__(self, secrets_dir: str | Path, *, cipher: Optional[Cipher] = None):
        self.secrets_dir = Path(secrets_dir)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        if cipher is not None:
            self.cipher: Cipher = cipher
        else:
            # 默认静态加密：cryptography 可用时走 Fernet（Argon2id 派生，固定 MASTER_SALT），
            # 不再发明文降级告警；仅当 cryptography 真正缺失时才回退 PlaintextCipher。
            try:
                import cryptography  # 守卫：缺 cryptography 时降级明文
                self.cipher = FernetCipher(
                    derive_fernet_key(
                        _default_passphrase(str(self.secrets_dir)),
                        salt=MASTER_SALT,
                    )[0]
                )
            except Exception:
                self.cipher = PlaintextCipher()

    def _path(self, provider: str) -> Path:
        # provider 仅作文件名片段，需安全化避免路径穿越
        safe = "".join(c for c in provider if c.isalnum() or c in "-_")
        return self.secrets_dir / f"{safe}.fernet"

    def get(self, provider: str) -> Optional[str]:
        p = self._path(provider)
        if not p.exists():
            return None
        try:
            blob = p.read_bytes()
            return self.cipher.decrypt(blob).decode("utf-8")
        except Exception:
            return None

    def set(self, provider: str, key: str) -> None:
        p = self._path(provider)
        p.write_bytes(self.cipher.encrypt(key.encode("utf-8")))
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
    """默认密钥目录：本 secrets 包所在目录（spec 约定 secrets/<provider>.fernet）。"""
    return Path(__file__).parent


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
def get_api_key(
    provider: str,
    *,
    backend: Optional[KeyBackend] = None,
    secrets_dir: Optional[str | Path] = None,
) -> str:
    """取回 provider 密钥。所有后端均无 → A040003（密钥缺失或无效）。"""
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
    """写入 provider 密钥（优先 keyring，否则文件保险库）。"""
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
    """
    if backend is not None:
        val = backend.get(provider)
        return {
            "provider": provider,
            "has_key": bool(val),
            "stored_in": backend.location(),
            "key_tail": (val[-4:] if val else None),
        }
    stored_in: Optional[str] = None
    key_tail: Optional[str] = None
    has = False
    for b in _default_backends(secrets_dir):
        val = b.get(provider)
        if val:
            has = True
            stored_in = b.location()
            key_tail = val[-4:]
            break
    return {"provider": provider, "has_key": has, "stored_in": stored_in, "key_tail": key_tail}
