"""
================================================================================
Heimdall 交易平台 - 多空不对称只读取证工具模块 (Asymmetric Evidence Tools)
================================================================================

【为什么需要为多头和空头研究员配备不同的只读取证工具？】：
1. 在传统多 Agent 辩论中，多头与空头只看到同一份技术指标摘要，容易陷入“互相复述相反结论”的同质化辩论；
2. 本模块基于原始 OHLCV K 线与衍生品资金费率，用纯确定性算法分别提取两个不同维度的微结构证据：
   - 多头动量取证工具 (`inspect_bull_momentum_evidence`)：聚焦连续抬高低点、阳线占比、放量突破量比、距 20 根高点距离；
   - 空头衰竭取证工具 (`inspect_bear_exhaustion_evidence`)：聚焦近 3 根上影线抛压比、高点下移、拉升缩量背离、多头拥挤资金费率。
3. 所有工具均为纯本地只读计算，0 外部副作用、0 Token 额外消耗，结果直接注入对应研究员的 Prompt 与规则兜底中。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全将任意 K 线数值字段转为 float，异常时返回默认值"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def inspect_bull_momentum_evidence(klines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    多头专属只读取证工具：从原始 OHLCV K 线中提取上涨延续与放量突破证据。

    计算维度：
    - `higher_lows_count_5bar`：近 5 根已完成 K 线中低点逐级抬高的次数（最多 4 次）
    - `green_candles_5bar`：近 5 根 K 线中的阳线根数
    - `volume_expansion_ratio`：近 3 根成交量均值 / 前 10 根基准成交量均值（量比）
    - `distance_to_20bar_high_pct`：最新收盘价距离近 20 根最高价的百分比距离
    - `breakout_imminent`：是否同时满足逼近前高（<=0.8%）且放量（量比 >=1.15）
    """
    if not klines or len(klines) < 5:
        logger.debug(
            "[多头取证工具] K 线数量不足 (bars=%d < 5)，跳过微结构动量取证",
            len(klines) if klines else 0,
        )
        return {
            "available": False,
            "reason": "insufficient_klines",
            "bars_checked": len(klines) if klines else 0,
        }

    # 取最近最多 20 根 K 线作为局部结构观察窗口
    recent = klines[-20:] if len(klines) >= 20 else klines
    lows = [_safe_float(k.get("low", k.get("l"))) for k in recent]
    highs = [_safe_float(k.get("high", k.get("h"))) for k in recent]
    opens = [_safe_float(k.get("open", k.get("o"))) for k in recent]
    closes = [_safe_float(k.get("close", k.get("c"))) for k in recent]
    volumes = [_safe_float(k.get("volume", k.get("v"))) for k in recent]

    # 1. 统计近 5 根 K 线低点抬高次数（衡量多头承接力度）
    last5_lows = lows[-5:]
    higher_lows_count = sum(
        1 for i in range(1, len(last5_lows)) if last5_lows[i] > last5_lows[i - 1]
    )

    # 2. 统计近 5 根 K 线阳线数量
    last5_green = sum(
        1 for o, c in zip(opens[-5:], closes[-5:]) if c >= o
    )

    # 3. 计算量比（近 3 根均量 / 此前最多 10 根基准均量）
    recent_vol = sum(volumes[-3:]) / max(1, len(volumes[-3:]))
    baseline_vols = volumes[-13:-3] if len(volumes) >= 13 else volumes[:-3]
    baseline_vol = (sum(baseline_vols) / len(baseline_vols)) if baseline_vols else recent_vol
    volume_expansion_ratio = round(recent_vol / baseline_vol, 3) if baseline_vol > 0 else 1.0

    # 4. 计算当前收盘价距离 20 根区间高点的百分比距离
    period_high = max(highs) if highs else 0.0
    last_close = closes[-1] if closes else 0.0
    distance_to_high_pct = (
        round(((period_high - last_close) / period_high) * 100.0, 3)
        if period_high > 0
        else 0.0
    )

    breakout_imminent = distance_to_high_pct <= 0.8 and volume_expansion_ratio >= 1.15
    result = {
        "available": True,
        "bars_checked": len(recent),
        "higher_lows_count_5bar": higher_lows_count,
        "green_candles_5bar": last5_green,
        "volume_expansion_ratio": volume_expansion_ratio,
        "distance_to_20bar_high_pct": distance_to_high_pct,
        "breakout_imminent": breakout_imminent,
    }
    logger.info(
        "[多头取证工具] 采样=%d根 | 低点抬高=%d/4 | 阳线=%d/5 | 量比=%.2fx | 距前高=%.2f%% | 临近突破=%s",
        len(recent),
        higher_lows_count,
        last5_green,
        volume_expansion_ratio,
        distance_to_high_pct,
        breakout_imminent,
    )
    return result


def inspect_bear_exhaustion_evidence(
    klines: List[Dict[str, Any]],
    annualized_funding: float = 0.0,
) -> Dict[str, Any]:
    """
    空头专属只读取证工具：从原始 OHLCV K 线与资金费率中提取见顶衰竭、上影线抛压与多头拥挤证据。

    计算维度：
    - `avg_upper_wick_ratio_3bar`：近 3 根 K 线的平均上影线占全振幅比例（>=0.4 说明上方抛压沉重）
    - `lower_highs_count_5bar`：近 5 根 K 线高点逐级下移次数
    - `red_candles_5bar`：近 5 根 K 线中的阴线根数
    - `volume_fade_on_rally`：价格上涨但成交量萎缩（量价顶背离）
    - `crowded_long_funding`：年化资金费率是否 > 30%（多头杠杆拥挤预警）
    - `exhaustion_warning`：综合衰竭预警布尔标记
    """
    if not klines or len(klines) < 5:
        logger.debug(
            "[空头取证工具] K 线数量不足 (bars=%d < 5)，仅基于资金费率 (%.2f%%) 输出基础判断",
            len(klines) if klines else 0,
            annualized_funding,
        )
        return {
            "available": False,
            "reason": "insufficient_klines",
            "bars_checked": len(klines) if klines else 0,
            "crowded_long_funding": annualized_funding > 30.0,
        }

    recent = klines[-20:] if len(klines) >= 20 else klines
    highs = [_safe_float(k.get("high", k.get("h"))) for k in recent]
    lows = [_safe_float(k.get("low", k.get("l"))) for k in recent]
    opens = [_safe_float(k.get("open", k.get("o"))) for k in recent]
    closes = [_safe_float(k.get("close", k.get("c"))) for k in recent]
    volumes = [_safe_float(k.get("volume", k.get("v"))) for k in recent]

    # 1. 计算近 3 根 K 线上影线占整根振幅的比例均值（识别冲高回落与派发抛压）
    wick_ratios: List[float] = []
    for h, l, o, c in zip(highs[-3:], lows[-3:], opens[-3:], closes[-3:]):
        bar_range = max(h - l, 1e-9)
        upper_wick = max(0.0, h - max(o, c))
        wick_ratios.append(upper_wick / bar_range)
    avg_upper_wick_ratio = round(sum(wick_ratios) / len(wick_ratios), 3)

    # 2. 统计近 5 根 K 线高点下移次数与阴线数量
    last5_highs = highs[-5:]
    lower_highs_count = sum(
        1 for i in range(1, len(last5_highs)) if last5_highs[i] < last5_highs[i - 1]
    )
    last5_red = sum(
        1 for o, c in zip(opens[-5:], closes[-5:]) if c < o
    )

    # 3. 检测量价顶背离：近 3 根价格上涨超 0.5% 但均量较前 5 根萎缩 15% 以上
    price_change_3bar = (
        ((closes[-1] - closes[-4]) / closes[-4]) * 100.0
        if len(closes) >= 4 and closes[-4] > 0
        else 0.0
    )
    recent_vol = sum(volumes[-3:]) / 3.0
    prev_vols = volumes[-8:-3] if len(volumes) >= 8 else volumes[:-3]
    prev_vol = (sum(prev_vols) / len(prev_vols)) if prev_vols else recent_vol
    volume_fade_on_rally = bool(
        price_change_3bar > 0.5 and prev_vol > 0 and (recent_vol / prev_vol) < 0.85
    )

    exhaustion_warning = avg_upper_wick_ratio >= 0.4 or volume_fade_on_rally or annualized_funding > 40.0
    result = {
        "available": True,
        "bars_checked": len(recent),
        "avg_upper_wick_ratio_3bar": avg_upper_wick_ratio,
        "lower_highs_count_5bar": lower_highs_count,
        "red_candles_5bar": last5_red,
        "volume_fade_on_rally": volume_fade_on_rally,
        "annualized_funding_pct": round(annualized_funding, 2),
        "crowded_long_funding": annualized_funding > 30.0,
        "exhaustion_warning": exhaustion_warning,
    }
    logger.info(
        "[空头取证工具] 采样=%d根 | 上影线均值=%.2f | 高点下移=%d/4 | 缩量背离=%s | 年化费率=%.2f%% | 衰竭预警=%s",
        len(recent),
        avg_upper_wick_ratio,
        lower_highs_count,
        volume_fade_on_rally,
        annualized_funding,
        exhaustion_warning,
    )
    return result
