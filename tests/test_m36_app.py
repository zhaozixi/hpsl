"""M36 / P1-P2：REST 自定义 msg、健康标记、readyz 不落探针、版本一致性。"""

from pathlib import Path

import hpsl
from hpsl import app as app_mod, projects
from hpsl.app import app


def _project(client, csrf, name="应用层"):
    r = client.post("/api/v1/projects", headers=csrf, json={"name": name, "goal": "g"})
    return r.json()["data"]["id"]


def test_version_consistent():
    assert hpsl.__version__ == "0.1.0"
    assert app is not None and app.version == "0.1.0"


def test_rest_error_keeps_custom_msg(client, csrf_headers):
    _project(client, csrf_headers)
    uid = client.post("/api/v1/units", headers=csrf_headers, json={"title": "U"}).json()["data"]["id"]
    r = client.post(
        f"/api/v1/units/{uid}/close",
        headers=csrf_headers,
        json={"confirmed_score": 0.8},
    )
    body = r.json()
    assert body["code"] == "A020001"
    # 自定义 msg 必须保留（此前被 fail(code) 的注册表文案覆盖）
    assert "JUDGE" in body["msg"]


def test_readyz_does_not_leave_probe(client, csrf_headers):
    pid = _project(client, csrf_headers, "readyz")
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["phase"] == "M36"
    probe = Path(projects._config.project_dir(pid)) / ".hpsl-readyz-probe"
    assert not probe.exists()
