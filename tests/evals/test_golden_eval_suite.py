"""
================================================================================
Heimdall 交易平台 - 25 场景多 Agent 黄金评测集回归套件 (Golden Eval Suite)
================================================================================

【评测目的】：
每次升级大模型版本、修改 Prompt、调整多空取证工具或风控阈值前，自动回放 25 个覆盖五大典型
市场状态的固定历史快照，硬性断言：
1. 场景覆盖度：5 大类别共 25 个独立场景全部加载成功；
2. 高危拦截率：极端插针、跨所 DEGRADED/ISOLATED、日内回撤熔断场景 100% 在前置门控 0-Token 拦截；
3. 趋势与陷阱纪律：顺势突破不逆势接飞刀，高位诱多陷阱与高正资金费拥挤不盲目追多；
4. 窄幅震荡防磨损：低波动箱体震荡保持 NEUTRAL 观望；
5. 结构契约完整性：100% 产出合法可观测状态与裁判证据审计字段，且端到端 P95 延迟受控。
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

from heimdall.agents.graph import MultiAgentPipeline
from heimdall.agents.state import MultiAgentState, TaskLifecycleStatus
from heimdall.llm.client import LLMService
from heimdall.risk.guard import HardRiskGuard

SCENARIOS_DIR = Path(__file__).resolve().parent / "golden_scenarios"


def _synthesize_klines(pattern: Dict[str, Any]) -> List[Dict[str, Any]]:
    """根据黄金场景 JSON 中的形态规格生成确定性 OHLCV K 线序列"""
    ptype = pattern["type"]
    bars = int(pattern.get("bars", 35))
    start = float(pattern.get("start", 3000.0))
    klines: List[Dict[str, Any]] = []

    if ptype == "uptrend":
        step = float(pattern.get("step", 4.0))
        vol_boost = bool(pattern.get("vol_boost", True))
        price = start
        for i in range(bars):
            o = price
            c = price + step
            h = c + abs(step) * 0.25
            l = o - abs(step) * 0.15
            v = 100.0 + (45.0 if (vol_boost and i >= bars - 3) else 0.0)
            klines.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
            price = c

    elif ptype == "downtrend":
        step = float(pattern.get("step", -4.0))
        price = start
        for i in range(bars):
            o = price
            c = price + step
            h = o + abs(step) * 0.15
            l = c - abs(step) * 0.25
            v = 110.0
            klines.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
            price = c

    elif ptype == "upper_wick_trap":
        # 前段温和反弹，末尾 5 根在阻力位出现超长上影线 + 缩量滞涨
        price = start
        for i in range(bars):
            if i < bars - 5:
                o = price
                c = price * (1.0 + 0.0005 * (1 if i % 2 == 0 else -0.4))
                h = max(o, c) * 1.001
                l = min(o, c) * 0.999
                v = 140.0
            else:
                o = price
                c = price * 0.9992
                h = price * 1.012  # 明显长上影线抛压
                l = c * 0.999
                v = 85.0           # 缩量背离
            klines.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
            price = c

    elif ptype == "flash_spike":
        # 正常平稳后最后一根出现 6% 暴跌插针（触发 FlashCrashDetector）
        price = start
        for i in range(bars):
            if i < bars - 1:
                o = price
                c = price * 1.0005
                h = c * 1.001
                l = o * 0.999
                v = 100.0
            else:
                o = price
                c = price * 0.94
                h = price * 1.002
                l = price * 0.915
                v = 500.0
            klines.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
            price = c

    else:  # range_flat：窄幅低波动箱体震荡
        amp = float(pattern.get("amp", start * 0.002))
        for i in range(bars):
            mid = start + math.sin(i * 0.9) * amp
            o = mid - amp * 0.1
            c = mid + amp * 0.1 * (1 if i % 2 == 0 else -1)
            h = max(o, c) + amp * 0.15
            l = min(o, c) - amp * 0.15
            klines.append({"open": o, "high": h, "low": l, "close": c, "volume": 100.0})

    return klines


def _load_all_golden_scenarios() -> List[Dict[str, Any]]:
    """加载 tests/evals/golden_scenarios/ 目录下所有 JSON 场景快照"""
    scenarios: List[Dict[str, Any]] = []
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        items = json.loads(path.read_text(encoding="utf-8"))
        scenarios.extend(items)
    return scenarios


def test_golden_eval_suite_25_scenarios():
    """回放全部 25 个黄金评测场景并校验安全拦截率、方向纪律与 P95 延迟"""
    async def _run():
        scenarios = _load_all_golden_scenarios()
        assert len(scenarios) >= 25, f"黄金评测集场景数不足 25 个，当前仅 {len(scenarios)}"

        categories = {s["category"] for s in scenarios}
        assert categories == {
            "trend_breakout",
            "bull_trap_exhaustion",
            "high_risk_blackout",
            "funding_squeeze",
            "range_chop",
        }

        offline_llm = LLMService(allow_env_fallback=False)
        offline_llm.is_enabled = False
        offline_llm.ensure_credentials = AsyncMock(return_value=False)

        guard = HardRiskGuard(
            daily_drawdown_limit_pct=0.05,
            enable_macro_guard=False,
            enable_flash_guard=True,
        )
        pipeline = MultiAgentPipeline(hard_guard=guard, llm_service=offline_llm)

        latencies_ms: List[float] = []
        high_risk_total = 0
        high_risk_blocked = 0
        passed_direction = 0

        for sc in scenarios:
            klines = _synthesize_klines(sc["kline_pattern"])
            state = MultiAgentState(
                symbol=sc["symbol"],
                account_equity=float(sc["account_equity"]),
                current_daily_drawdown_pct=float(sc["daily_drawdown_pct"]),
                raw_klines=klines,
                raw_funding=sc["funding"],
                raw_guard_status=sc["guard_status"],
            )
            t0 = time.perf_counter()
            out = await pipeline.run(state)
            latencies_ms.append((time.perf_counter() - t0) * 1000)

            # 1. 结构完整度检查：无论放行或拦截，均产出完整 debate_synthesis 与 evidence_audit
            assert out.debate_synthesis is not None
            assert out.debate_synthesis.get("evidence_audit")

            action = out.debate_synthesis.get("consensus_action")

            # 2. 高危场景必须 100% 被前置门控 0-Token 拦截
            if sc["expected_blocked"]:
                high_risk_total += 1
                assert out.status == TaskLifecycleStatus.REJECTED, f"{sc['scenario_id']} 应被高危拦截"
                assert out.metrics["total_tokens"] == 0, f"{sc['scenario_id']} 应 0-Token 短路"
                assert out.step_traces[0].step_name == "Step0_PreFlightGate"
                high_risk_blocked += 1

            # 3. 方向与纪律检查（允许在预期安全集合内）
            if action in sc["expected_actions"]:
                passed_direction += 1

        # 硬性回归门槛断言
        assert high_risk_total == 5 and high_risk_blocked == 5, "高危场景安全拦截率必须为 100%"
        accuracy = passed_direction / len(scenarios)
        assert accuracy >= 0.92, f"黄金评测集方向与纪律匹配率 {accuracy:.1%} 低于 92% 门槛"

        latencies_ms.sort()
        p95_ms = latencies_ms[int(len(latencies_ms) * 0.95) - 1]
        assert p95_ms < 500.0, f"离线评测 P95 延迟过大: {p95_ms:.1f}ms"

    asyncio.run(_run())
