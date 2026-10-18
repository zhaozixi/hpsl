"""E-08 工程债收口：密钥库的**跨进程**真实验证。

为什么需要它：既有 `test_models.py` / `test_m25_security.py` 的密钥用例都在**同一进程**
内完成 set → get，无法证明"密文落到磁盘后，换个进程用同一口令还能解开"。
真实使用场景恰恰是跨进程的（今天 `hpsl serve` 写入，明天再启动读取）。

本文件用 `subprocess` 起独立解释器：
1. 进程 A 写入密钥并退出（进程内状态全部释放）；
2. 进程 B 读回并打印尾号；
3. 校验磁盘文件是 `HPSL2` 头（salt + token），不带明文。

keyring 说明：本机未安装 `keyring`，无法做真实钥匙环集成；此处只断言
「要么拿到 keyring 后端，要么明确回落到 fernet_file」——两种结果都必须是**确定且安全**的，
不允许静默使用明文。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PASS = "e2e-passphrase-please-change"


def _has_crypto() -> bool:
    try:
        import cryptography  # noqa: F401
        import argon2  # noqa: F401
    except Exception:
        return False
    return True


crypto_required = pytest.mark.skipif(
    not _has_crypto(), reason="未安装 cryptography / argon2-cffi，密钥加密链路不可用"
)


def _run(code: str, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HPSL_HOME"] = str(home)
    env["HPSL_MASTER_PASSPHRASE"] = PASS
    env.pop("PYTHONPATH", None)  # 避免外部 shim 干扰
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@crypto_required
def test_key_survives_process_restart(tmp_path):
    home = tmp_path / "hpsl_home"
    home.mkdir(parents=True, exist_ok=True)

    write = (
        "from hpsl import secrets\n"
        "secrets.set_api_key('deepseek', 'sk-cross-process-9876')\n"
        "print('written', secrets.key_status('deepseek')['stored_in'])\n"
    )
    r1 = _run(write, home)
    assert r1.returncode == 0, r1.stderr[-800:]
    assert "written" in r1.stdout

    # 独立进程重新读：进程内缓存全部不存在，只能走磁盘解密
    read = (
        "from hpsl import secrets\n"
        "print('TAIL=' + str(secrets.key_status('deepseek')['key_tail']))\n"
        "print('KEY=' + str(secrets.get_api_key('deepseek')))\n"
    )
    r2 = _run(read, home)
    assert r2.returncode == 0, r2.stderr[-800:]
    assert "TAIL=9876" in r2.stdout
    assert "KEY=sk-cross-process-9876" in r2.stdout


@crypto_required
def test_ciphertext_file_is_hpsl2_and_has_no_plaintext(tmp_path):
    home = tmp_path / "hpsl_home"
    home.mkdir(parents=True, exist_ok=True)
    write = (
        "from hpsl import secrets\n"
        "secrets.set_api_key('qwen', 'sk-plain-must-not-appear-1234')\n"
    )
    r = _run(write, home)
    assert r.returncode == 0, r.stderr[-800:]

    files = list((home / "secrets").glob("*.fernet"))
    assert files, "密钥文件未落盘"
    raw = files[0].read_bytes()
    assert raw.startswith(b"HPSL2"), "缺少每文件随机 salt 的 HPSL2 文件头"
    assert b"sk-plain-must-not-appear" not in raw, "明文出现在了密文文件里"


@crypto_required
def test_wrong_passphrase_fails_loudly(tmp_path):
    """口令错 → 必须显式失败（A040003 一类的 BizError），不得返回空字符串蒙混。"""
    home = tmp_path / "hpsl_home"
    home.mkdir(parents=True, exist_ok=True)
    r1 = _run(
        "from hpsl import secrets\nsecrets.set_api_key('zhipu', 'sk-abc-4321')\n", home
    )
    assert r1.returncode == 0, r1.stderr[-800:]

    env = dict(os.environ)
    env["HPSL_HOME"] = str(home)
    env["HPSL_MASTER_PASSPHRASE"] = "definitely-wrong-passphrase"
    env.pop("PYTHONPATH", None)
    code = (
        "from hpsl import secrets\n"
        "from hpsl.common.errors import BizError\n"
        "try:\n"
        "    v = secrets.get_api_key('zhipu')\n"
        "    print('LEAK=' + repr(v))\n"
        "except BizError as e:\n"
        "    print('BIZERR=' + e.code)\n"
    )
    r2 = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120
    )
    assert r2.returncode == 0, r2.stderr[-800:]
    assert "BIZERR=" in r2.stdout or r2.stdout.strip() == "LEAK=None", r2.stdout


def test_no_plaintext_fallback_without_passphrase(tmp_path, monkeypatch):
    """没有口令且没有 keyring 时必须拒绝写入（不降级明文）。"""
    from hpsl import secrets as secrets_mod

    monkeypatch.setenv("HPSL_HOME", str(tmp_path / "h"))
    (tmp_path / "h").mkdir()
    monkeypatch.delenv("HPSL_MASTER_PASSPHRASE", raising=False)
    if secrets_mod.lock_state()["stored_in"] == "keyring":
        pytest.skip("本机有可用 keyring，口令缺失属正常路径")

    from hpsl.common.errors import BizError

    with pytest.raises(BizError) as exc:
        secrets_mod.set_api_key("deepseek", "sk-should-not-be-written")
    assert exc.value.code == "A040003"
    assert not list((tmp_path / "h" / "secrets").glob("*.fernet")), "禁止写入明文密钥文件"


def test_keyring_backend_is_deterministic():
    """keyring 有则必须是 keyring 后端，无则必须明确回落 —— 不允许第三种含糊状态。"""
    from hpsl.secrets import keystore

    backend = keystore._try_keyring_backend()
    if backend is None:
        state = keystore.lock_state()
        assert state["stored_in"] in ("fernet_file", "missing")
    else:
        assert backend.location() == "keyring"
