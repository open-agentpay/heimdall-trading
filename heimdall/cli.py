"""
Heimdall 交易平台 - 终端命令行交互工具 (CLI Entrypoint)
采用 Python 标准库 argparse 实现，具备零额外三方 CLI 依赖的高可用性：
1. 启动 Web 控制台中台服务 (python3 -m heimdall.cli serve)；
2. 运行多智能体仿真与撮合闭环演练 (python3 -m heimdall.cli run-demo)；
3. 输出当前平台版本号 (python3 -m heimdall.cli version)。
"""

import sys
import argparse
import asyncio
import logging
from typing import Optional
from heimdall import __version__
from heimdall.engine.bot_manager import BotManager
from heimdall.models.schemas import StrategyType
from heimdall.bifrost.client import BifrostClient

# 配置命令行交互日志输出格式，并默认挂载敏感凭据脱敏过滤器
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
from heimdall.utils.logging_security import install_secret_redaction_filter
install_secret_redaction_filter()
logger = logging.getLogger("heimdall.cli")


def cmd_serve(args: argparse.Namespace):
    """
    启动 FastAPI 后端服务与调度中台
    """
    import uvicorn
    print(f"🚀 正在启动 Heimdall 交易平台服务端: http://{args.host}:{args.port}")
    uvicorn.run("heimdall.api.server:app", host=args.host, port=args.port, reload=args.reload)


def cmd_run_demo(args: argparse.Namespace):
    """
    执行一次完整的端到端演练：
    Bifrost 数据拉取 -> Analyst 研判 -> 多空辩论 -> 策略生成 -> 软/硬两道风控 -> 沙盒撮合 -> NAV 计算
    """
    async def _execute_demo():
        print("=" * 70)
        print(f"  Heimdall Multi-Agent Trading Demo | 标的: {args.symbol} | 本金: ${args.capital:,.2f}")
        print("=" * 70)

        # 1. 构造带有确定性平滑行情返回的 Bifrost 测试客户端
        class DemoBifrostClient(BifrostClient):
            async def get_klines(self, symbol, interval="15m", limit=60, as_of_ts=None):
                # 构造平滑多头推进行情（3000 -> 3135）
                base_price = 3000.0
                klines = []
                for i in range(limit):
                    price = base_price + (i * 2.2)
                    klines.append({"timestamp": 1700000000 + i * 900, "close": price, "volume": 120.0})
                return klines

            async def get_funding_rate(self, symbol):
                return {"symbol": symbol, "funding_rate": 0.0001, "annualized_rate": 0.1095}

            async def check_cross_exchange_guard(self, symbol):
                return {"status": "HEALTHY", "spread_pct": 0.02}

        bifrost = DemoBifrostClient()
        manager = BotManager(bifrost_client=bifrost)

        # 2. 注册并启动测试机器人实例
        bot = manager.create_bot(
            bot_id="demo_bot_01",
            name=f"{args.symbol} 多空对抗自适应智能体",
            symbol=args.symbol,
            strategy_type=StrategyType.BULL_BEAR_DEBATE,
            allocated_capital=args.capital,
            max_drawdown_limit=0.05,
        )
        manager.start_bot(bot.bot_id)

        # 3. 触发一次完整的决策步
        print("\n[1/4] 触发多智能体决策闭环...")
        record = await bot.step()

        # 4. 打印思考链路
        print("\n[2/4] 多智能体协同思考链明细 (Chain-of-Thought):")
        for log in record.get("thinking_logs", []):
            print(f"  • {log}")

        # 5. 打印成交流水与风控结果
        print("\n[3/4] 执行与风控结果:")
        print(f"  • 风控是否核准: {'✅ 全票通过放行' if record.get('risk_approved') else '❌ 拦截'}")
        print(f"  • 成交流水编号: {record.get('execution_id')}")

        # 6. 打印最新 NAV 净值与资产状况
        print("\n[4/4] 投资组合绩效指标 (NAV Engine):")
        metrics = bot.nav_engine.calculate_performance_metrics()
        for k, v in metrics.items():
            print(f"  • {k}: {v}")

        print("\n" + "=" * 70)
        print("🎉 演练圆满完成！软决策推理与代码层硬风控双轨机制运转正常。")
        print("=" * 70)

    asyncio.run(_execute_demo())


def cmd_backtest(args: argparse.Namespace):
    """运行零前视历史回测"""
    async def _run_bt():
        from heimdall.strategies import StrategyRouter
        from heimdall.backtest import BacktestEngine, HistoricalDataFeed

        stype = StrategyType(args.strategy.upper())
        strategy = StrategyRouter.create(stype, bot_id=f"bt_{stype.value.lower()}")
        feed = HistoricalDataFeed.generate_synthetic(
            symbol=args.symbol,
            bars=args.bars,
            timeframe=args.timeframe,
        )
        engine = BacktestEngine()
        report = await engine.run(
            strategy=strategy,
            feed=feed,
            initial_capital=args.capital,
            warmup_bars=args.warmup,
        )
        print("=" * 70)
        print(f"  Heimdall Zero-Lookahead Backtest | {report.strategy_type.value} | {report.symbol} ({report.timeframe})")
        print("=" * 70)
        print(f"  • 回测 K 线数: {report.bars_evaluated} | 跨度天数: {report.calendar_days} 天")
        print(f"  • 初始本金: ${report.initial_capital:,.2f} -> 终态权益: ${report.final_equity:,.2f} (净盈亏: ${report.net_pnl_usd:+,.2f})")
        print(f"  • 累计收益: {report.cumulative_return_pct:+.2f}% | 最大回撤: {report.max_drawdown_pct:.2f}%")
        print(f"  • 年化夏普: {report.annualized_sharpe_ratio:.2f} | 年化索提诺: {report.annualized_sortino_ratio:.2f} | 卡玛: {report.calmar_ratio:.2f}")
        print(f"  • 总成交: {report.total_trades} 笔 (平仓 {report.closing_trades} 笔) | 胜率: {report.win_rate_pct:.1f}% | 盈亏比: {report.profit_factor:.2f}")
        print(f"  • 累计手续费: ${report.total_fees_usd:.2f} | 资金费净收益: ${report.total_funding_pnl_usd:+.2f}")
        print("=" * 70)

    asyncio.run(_run_bt())


def cmd_version(args: argparse.Namespace):
    """打印平台当前版本"""
    print(f"Heimdall Trading Platform Version: {__version__}")


def main():
    """CLI 主入口函数"""
    parser = argparse.ArgumentParser(
        prog="heimdall",
        description="Heimdall AI Agent 量化交易与资产管理平台命令行工具",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # 1. serve 命令
    serve_parser = subparsers.add_parser("serve", help="启动平台 Web API 服务端")
    serve_parser.add_argument("--host", default="0.0.0.0", help="监听网络主机地址")
    serve_parser.add_argument("--port", type=int, default=8080, help="监听端口")
    serve_parser.add_argument("--reload", action="store_true", help="热重载模式")
    serve_parser.set_defaults(func=cmd_serve)

    # 2. run-demo 命令
    demo_parser = subparsers.add_parser("run-demo", help="执行一次全链路多智能体撮合演练")
    demo_parser.add_argument("--symbol", default="ETH", help="演练交易标的")
    demo_parser.add_argument("--capital", type=float, default=10000.0, help="初始测试本金 (USD)")
    demo_parser.set_defaults(func=cmd_run_demo)

    # 3. backtest 命令
    bt_parser = subparsers.add_parser("backtest", help="运行零前视事件驱动历史回测")
    bt_parser.add_argument("--strategy", default="TREND_FOLLOW", help="策略类型: TREND_FOLLOW / GRID / FUNDING_ARB / BULL_BEAR_DEBATE")
    bt_parser.add_argument("--symbol", default="ETH", help="交易标的")
    bt_parser.add_argument("--timeframe", default="15m", help="K 线周期")
    bt_parser.add_argument("--bars", type=int, default=240, help="回测 K 线根数")
    bt_parser.add_argument("--warmup", type=int, default=30, help="预热 K 线根数")
    bt_parser.add_argument("--capital", type=float, default=10000.0, help="初始本金 (USD)")
    bt_parser.set_defaults(func=cmd_backtest)

    # 4. version 命令
    version_parser = subparsers.add_parser("version", help="查看平台版本")
    version_parser.set_defaults(func=cmd_version)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
