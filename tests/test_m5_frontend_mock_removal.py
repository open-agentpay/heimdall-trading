"""
================================================================================
回归测试：2026-11-21 代码审计中危问题 M5 —— 前端不再展示假数据 / 后端标注 K 线数据来源
================================================================================
1. 前端已移除 mockData.js，任何组件不得再引用 INITIAL_AGENTS / INITIAL_KLINES；
2. 前端组件不得残留写死的标的价格、编造的“风控全票核准”与看多兜底文案；
3. 离线仿真 K 线带 source=offline_fallback 标记；
4. /api/v1/market/klines 返回 provenance：真实行情 LIVE、离线仿真 SIMULATED、无数据 UNAVAILABLE；
5. 其他页面（终端面板、审计轨迹、净值、风控雷达、情绪、密钥金库）不得残留编造的示例数据与默认结论；
6. 净值接口在没有亏损交易时不再把盈亏比写成 2.0 / 1.0。
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from heimdall.api.server import bot_manager, get_bot_nav, get_market_klines
from heimdall.data.market_data import OfflineFallbackDataProvider

ROOT = Path(__file__).resolve().parent.parent
WEB_SRC = ROOT / "web" / "src"


# -----------------------------------------------------------------------------
# 1 & 2. 前端源码静态检查
# -----------------------------------------------------------------------------

def _web_sources():
    return [p for p in WEB_SRC.rglob("*") if p.suffix in (".js", ".jsx")]


def test_mock_data_module_removed():
    assert not (WEB_SRC / "data" / "mockData.js").exists()
    offenders = [
        str(p.relative_to(ROOT))
        for p in _web_sources()
        if any(tok in p.read_text(encoding="utf-8") for tok in ("from './data/mockData'", "from '../data/mockData'", "INITIAL_AGENTS", "INITIAL_KLINES"))
    ]
    assert offenders == [], f"仍有组件引用假数据: {offenders}"


@pytest.mark.parametrize(
    "rel_path,forbidden",
    [
        ("web/src/components/DecisionArena.jsx", ["defaultPrices", "?? true", "7/7 铁律全票核准", "现货买盘真实，突破阻力暴拉赔率极高"]),
        ("web/src/components/KlineChart.jsx", ["SYMBOL_PRICES", "Paper Sandboxed", "TV LIVE: BINANCE WS"]),
        ("web/src/components/AgentModal.jsx", ["'45ms'", "cot_agent_"]),
        ("web/src/App.jsx", ["?? 0.45", "'ACCUMULATION';", "2.17:1", "盈亏比优于 2.0:1", "passed ?? true"]),
        ("web/src/components/TerminalPanel.jsx", ["defaultLogs", "1.0422", "RSI 为 58.4", "1,250 万", "PaperEngine Live", "3120.00", "40 + idx * 25", "ARMED"]),
        ("web/src/components/AuditTracesView.jsx", ["fallbackTraces", "1,744", "+0.45", "LangGraph", "OpenTelemetry", "risk_reward_ratio_ge_1_2", "latency: 42.1", "(action !== 'NEUTRAL')"]),
        ("web/src/components/NavView.jsx", ["total_equity_usd: 10000.0", "优秀 (&gt;2.0)", "正期望盈利模型", "100% 现金就绪", "nav: 1.0000"]),
        ("web/src/components/RiskRadarView.jsx", ["const overallStatus = radarData?.overall_status ||", "全防线健康巡航中", "discrepancies?.length || 1"]),
        ("web/src/components/SentimentView.jsx", [": 0.0;", "whale_activity || 'NEUTRAL'", "crowding_risk || 'BALANCED'"]),
        ("web/src/components/KeyVaultModal.jsx", ["|| 80)", "latency: 105", "硬件认证"]),
    ],
)
def test_frontend_has_no_fabricated_fallbacks(rel_path, forbidden):
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    found = [tok for tok in forbidden if tok in text]
    assert found == [], f"{rel_path} 仍含编造的默认值 / 文案: {found}"


def test_agent_roles_hold_no_market_numbers():
    text = (WEB_SRC / "data" / "agentRoles.js").read_text(encoding="utf-8")
    for tok in ("keyThesis: \"", "evidence: [\n      \"", "1,744", "confidence: 0."):
        assert tok not in text, f"agentRoles.js 不应包含行情数值或分析结论: {tok!r}"


# -----------------------------------------------------------------------------
# 3. 离线仿真 K 线来源标记
# -----------------------------------------------------------------------------

def test_offline_klines_are_tagged():
    klines = asyncio.run(OfflineFallbackDataProvider().get_klines("ETH", limit=5))
    assert len(klines) == 5
    assert all(k.get("source") == "offline_fallback" for k in klines)


# -----------------------------------------------------------------------------
# 4. 行情接口 provenance
# -----------------------------------------------------------------------------

def _call(return_value):
    with patch.object(bot_manager.bifrost_client, "get_klines", AsyncMock(return_value=return_value)):
        return asyncio.run(get_market_klines(symbol="ETH", interval="15m", limit=5, as_of_ts=None))


def test_klines_endpoint_reports_simulated_for_offline_fallback():
    offline = asyncio.run(OfflineFallbackDataProvider().get_klines("ETH", limit=5))
    res = _call(offline)
    assert res["provenance"] == "SIMULATED"
    assert res["sources"] == ["offline_fallback"]
    assert len(res["data"]) == 5


def test_klines_endpoint_reports_live_for_exchange_data():
    live = [{"timestamp": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 3100.0, "volume": 1.0, "source": "okx_swap"}]
    res = _call(live)
    assert res["provenance"] == "LIVE"
    assert res["sources"] == ["okx_swap"]


def test_klines_endpoint_reports_unavailable_when_empty():
    res = _call([])
    assert res["provenance"] == "UNAVAILABLE"
    assert res["data"] == []


# -----------------------------------------------------------------------------
# 6. 净值接口：无亏损交易时盈亏比为 None
# -----------------------------------------------------------------------------

def test_nav_profit_factor_is_none_without_losing_trades():
    bid = "bot_m5_pf_none"
    bot_manager.bots.pop(bid, None)
    bot_manager.create_bot(bot_id=bid, name="盈亏比测试", symbol="ETH")
    try:
        res = asyncio.run(get_bot_nav(bot_id=bid, caller_wallet=None))
        assert res["metrics"]["total_trades"] == 0
        assert res["metrics"]["profit_loss_ratio"] is None
    finally:
        bot_manager.bots.pop(bid, None)


def test_backend_has_no_fabricated_metric_fallbacks():
    server = (ROOT / "heimdall" / "api" / "server.py").read_text(encoding="utf-8")
    router = (ROOT / "heimdall" / "auth" / "router.py").read_text(encoding="utf-8")
    assert "(2.0 if profit_sum > 0 else 1.0)" not in server
    assert "latency or 85.0" not in router
