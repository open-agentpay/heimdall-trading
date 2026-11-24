"""
辩论策略开仓提案交易所写错导致无法下单的回归测试（2026-11-24）

现象：bot_4350（BINANCE · USDM_FUTURES · TESTNET · BULL_BEAR_DEBATE）在 2026-11-23 23:29（新加坡时间）
裁决 LONG、风控通过，但净值里没有订单。后端日志：
    [OMS 发单前拦截] 原因=OMS 规格校验拦截: 未找到 HYPERLIQUID/USDM_FUTURES/BNB 的标的规格
原因：多 Agent 流水线里的 StrategyAgent 把 trade_action.exchange 写死为 HYPERLIQUID；
OMS 按「提案上的交易所 / 市场 / 标的」查交易规格，币安机器人的规格登记在 BINANCE 名下，
测试网 / 实盘下单前又要求规格必须已知（P1），于是辩论策略的每一笔开仓都在发单前被拦截。
（平仓提案走 BaseStrategy._build_proposal，使用 ctx.exchange，不受影响；纯算法策略同理。）

修复：
1. DebateStrategyAdapter 把流水线产出的开仓提案改写为机器人自身的交易所（ctx.exchange）；
2. OrderExecutionService 下单前用 align_proposal_venue 再按机器人配置对齐交易所与市场类型（所有策略的兜底）。

覆盖：
1. 辩论适配器：流水线返回 HYPERLIQUID 提案时，开仓提案改写为 BINANCE；交易所本来一致时不改；
2. align_proposal_venue：不一致时改写并返回描述，一致时返回空列表；
3. 端到端：用修复后的提案走 OMS 规格校验，BINANCE/USDM_FUTURES/BNB 能查到规格，HYPERLIQUID 查不到（复现原问题）。
"""

import asyncio
from types import SimpleNamespace

from heimdall.engine.bot_services.order_execution import align_proposal_venue
from heimdall.execution.precision import InstrumentRegistry
from heimdall.models.schemas import ExchangeType, MarketType, OrderSide, PositionSide
from heimdall.strategies.base import StrategyContext
from heimdall.strategies.debate_adapter import DebateStrategyAdapter


def _make_adapter():
    """绕过 __init__（不创建真实 LLM 流水线），只装上本测试需要的属性"""
    adapter = DebateStrategyAdapter.__new__(DebateStrategyAdapter)
    adapter.bot_id = "bot_4350"
    adapter.config = {}
    adapter.debate_rounds = 1
    adapter.atr_stop_mult = 2.0
    adapter._repo_getter = None
    adapter.checkpoint_ttl_seconds = 300.0
    adapter.last_resumed_task_id = None
    adapter.pipeline = None  # 各用例自行装上返回固定结果的假流水线
    return adapter


def _ctx(exchange):
    return StrategyContext(
        bot_id="bot_4350",
        symbol="BNB",
        exchange=exchange,
        account_equity=5000.0,
        factors={"latest_price": 572.93, "atr14": 0.0, "regime": "TREND_UP"},
    )


def _proposal(adapter, exchange):
    """用策略基类的构造方法造一笔做多限价提案（与 2026-11-23 那笔参数一致）"""
    return adapter._build_proposal(
        ctx=_ctx(exchange),
        pos_side=PositionSide.LONG,
        order_side=OrderSide.BUY,
        target_price=572.64,
        size_in_usd=310.0,
        stop_loss_price=552.0,
        take_profit_price=605.0,
    )


def _final_state(proposal):
    return SimpleNamespace(
        analyst_brief={"regime": "TREND_UP", "latest_price": 572.93},
        sentiment_report=None,
        bull_thesis=None,
        bear_thesis=None,
        debate_synthesis={"consensus_action": "LONG", "final_confidence": 0.62},
        risk_review=None,
        step_traces=[],
        metrics={},
        thinking_logs=["[裁判] LONG 62%"],
        trade_proposal=proposal,
        checkpoints=[],
        task_id="t",
    )


# ----------------------------------------------------------------------
# 1. 辩论适配器
# ----------------------------------------------------------------------

def test_debate_adapter_rewrites_open_proposal_to_bot_exchange():
    adapter = _make_adapter()
    prop = _proposal(adapter, ExchangeType.HYPERLIQUID)  # 模拟 StrategyAgent 写死的 HYPERLIQUID
    adapter.pipeline = SimpleNamespace(run=lambda state: _async(_final_state(prop)))

    decision = asyncio.run(adapter.evaluate(_ctx(ExchangeType.BINANCE)))

    assert decision.action_type == "OPEN"
    assert len(decision.proposals) == 1
    assert decision.proposals[0].trade_action.exchange == ExchangeType.BINANCE


def test_debate_adapter_keeps_matching_exchange():
    adapter = _make_adapter()
    prop = _proposal(adapter, ExchangeType.HYPERLIQUID)
    adapter.pipeline = SimpleNamespace(run=lambda state: _async(_final_state(prop)))

    decision = asyncio.run(adapter.evaluate(_ctx(ExchangeType.HYPERLIQUID)))

    assert decision.proposals[0].trade_action.exchange == ExchangeType.HYPERLIQUID


async def _async(value):
    return value


# ----------------------------------------------------------------------
# 2. 下单前兜底对齐
# ----------------------------------------------------------------------

def test_align_proposal_venue_rewrites_mismatch_and_reports_it():
    adapter = _make_adapter()
    prop = _proposal(adapter, ExchangeType.HYPERLIQUID)
    prop.trade_action.market_type = MarketType.SPOT

    changed = align_proposal_venue([prop], ExchangeType.BINANCE, MarketType.USDM_FUTURES)

    assert prop.trade_action.exchange == ExchangeType.BINANCE
    assert prop.trade_action.market_type == MarketType.USDM_FUTURES
    assert len(changed) == 1
    assert "HYPERLIQUID/SPOT -> BINANCE/USDM_FUTURES" in changed[0]


def test_align_proposal_venue_silent_when_already_aligned():
    adapter = _make_adapter()
    prop = _proposal(adapter, ExchangeType.BINANCE)
    prop.trade_action.market_type = MarketType.USDM_FUTURES

    assert align_proposal_venue([prop], ExchangeType.BINANCE, MarketType.USDM_FUTURES) == []


# ----------------------------------------------------------------------
# 3. 规格查询：修复前查不到，修复后查得到
# ----------------------------------------------------------------------

def test_spec_lookup_uses_bot_exchange_after_fix():
    registry = InstrumentRegistry()
    # 复现原问题：内置规格表里没有 HYPERLIQUID/USDM_FUTURES/BNB
    assert not registry.has_spec("HYPERLIQUID", "BNB", MarketType.USDM_FUTURES)

    adapter = _make_adapter()
    prop = _proposal(adapter, ExchangeType.HYPERLIQUID)
    align_proposal_venue([prop], ExchangeType.BINANCE, MarketType.USDM_FUTURES)

    action = prop.trade_action
    assert registry.has_spec(action.exchange.value, action.symbol, action.market_type)
