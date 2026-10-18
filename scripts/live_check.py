"""真实联网自检（可选，不进入 pytest 默认套件）。

用途：用真实 Provider 密钥跑一遍「建项目 → 存密钥 → 建单元 → 角色调用 → 记账 → 对话落库」，
验证适配器、密钥库、记账、turn 与 HTTP 链路在真实网络下可用。

用法（PowerShell）：
    $env:DEEPSEEK_API_KEY = "sk-..."
    python scripts/live_check.py
    python scripts/live_check.py --model deepseek-flash --role clerk

退出码：0 成功；1 失败（打印 code/msg）。脚本不会把密钥写入任何持久文件：
HPSL_HOME 是临时目录，结束后删除；密钥只在进程内与临时加密文件中存在。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

# 环境必须在 import hpsl 之前设定
HPSL_HOME = tempfile.mkdtemp(prefix="hpsl-live-")
os.environ["HPSL_HOME"] = HPSL_HOME
os.environ.setdefault("HPSL_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
os.environ.setdefault("HPSL_MASTER_PASSPHRASE", "live-check-passphrase")
os.environ.setdefault(
    "HPSL_RATE_LIMITS",
    "process=1000,route=1000,llm_freq=1000,sse_concurrency=1000,write_freq=1000",
)

# 允许 `python scripts/live_check.py` 直接运行（把仓库根加入 sys.path）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from hpsl import db  # noqa: E402
from hpsl.app import app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--role", default="clerk")
    parser.add_argument("--text", default="测试：请用一句话说明 JSON 的用途。")
    parser.add_argument("--stream", action="store_true", help="额外验证 SSE 流式接口")
    args = parser.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("HPSL_LIVE_KEY")
    if not key:
        print("缺少 DEEPSEEK_API_KEY（或 HPSL_LIVE_KEY）环境变量", file=sys.stderr)
        return 1
    os.environ["HPSL_DEFAULT_MODEL"] = args.model

    client = TestClient(app)
    client.get("/")  # 触发 CSRF cookie
    token = client.cookies.get("hpsl_csrf")
    headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": token}

    def call(method: str, url: str, body: dict | None = None) -> dict:
        r = client.request(method, url, headers=headers, json=body or {})
        data = r.json()
        if data.get("code") != "0":
            raise RuntimeError(f"{method} {url} -> {data.get('code')}: {data.get('msg')}")
        return data.get("data")

    pid = None
    try:
        project = call("POST", "/api/v1/projects", {"name": "LiveCheck", "goal": "联网自检"})
        pid = project["id"]
        call("PUT", f"/api/v1/settings/keys/{args.provider}", {"key": key})
        unit = call("POST", "/api/v1/units", {"title": "联网自检单元"})

        result = call(
            "POST",
            f"/api/v1/roles/{args.role}/call",
            {"unit_id": unit["id"], "learner_input": args.text},
        )

        summary = {
            "provider": args.provider,
            "model": result["model"],
            "role": args.role,
            "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"],
            "cost_usd": result["cost_usd"],
            "cost_status": result["cost_status"],
            "text_head": (result["text"] or "")[:120],
            "llm_calls": db.query(pid, "SELECT count(*) c FROM llm_call")[0]["c"],
            "turns": db.query(pid, "SELECT count(*) c FROM turn")[0]["c"],
            "sessions": db.query(pid, "SELECT count(*) c FROM session")[0]["c"],
            "objects": db.count_user_objects(pid),
        }
        if args.stream:
            with client.stream(
                "POST",
                f"/api/v1/providers/{args.provider}/chat/stream",
                headers=headers,
                json={"messages": [{"role": "user", "content": "用一句话说明 JSON 的用途。"}]},
            ) as resp:
                sse = "".join(resp.iter_text())
            summary["stream_deltas"] = sse.count('"delta"')
            summary["stream_done"] = '"done": true' in sse
            summary["stream_cost_head"] = sse[-260:]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        ok = bool(result["text"]) and summary["llm_calls"] == 1 and summary["turns"] == 2
        return 0 if ok else 1
    except Exception as exc:  # 打印可读错误，便于定位
        print(f"LIVE CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(HPSL_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
