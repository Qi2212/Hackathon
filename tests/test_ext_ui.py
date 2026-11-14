"""前端工作台静态资源（P8）测试。"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from ext.analyzer import TaskAnalyzer
from ext.estimator import WorkloadEstimator
from ext.fabric_client import FabricClient
from ext.main import create_app


def make_client() -> TestClient:
    fabric = FabricClient(
        base_url="http://fabric.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                404,
                json={"error": "not_found", "detail": request.url.path, "request_id": "req_x"},
            )
        ),
    )
    return TestClient(create_app(fabric, analyzer=TaskAnalyzer(), estimator=WorkloadEstimator()))


def test_workbench_page_is_served():
    with make_client() as client:
        response = client.get("/ui/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "Campus GPU Fabric" in response.text
        assert "app.js" in response.text
        assert "styles.css" in response.text


def test_static_assets_are_served():
    with make_client() as client:
        script = client.get("/ui/app.js")
        styles = client.get("/ui/styles.css")
    assert script.status_code == 200
    assert "renderPlan" in script.text
    assert styles.status_code == 200
    assert "--accent" in styles.text


def test_root_redirects_to_workbench():
    with make_client() as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/ui/"
