"""
测试 7 大极端行情避险雷达聚合端点 /api/v1/market/radar
"""
from starlette.testclient import TestClient
from heimdall.api.server import app


def test_get_risk_radar_telemetry():
    client = TestClient(app)
    resp = client.get("/api/v1/market/radar?symbol=ETH")
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == 0
    assert data["symbol"] == "ETH"
    assert "rules" in data
    assert len(data["rules"]) == 7
    # 验证规则结构
    for rule in data["rules"]:
        assert "id" in rule
        assert "name" in rule
        assert "status" in rule
        assert "value" in rule
