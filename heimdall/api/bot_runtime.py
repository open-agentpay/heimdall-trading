"""
机器人启动运行时（Bot Runtime Start）

背景（2026-11-23 修复）
----------------------
机器人有两个"开始运行"的入口：

1. 启动接口 ``POST /api/v1/bots/{bot_id}/start``：做齐全部启动前检查，再创建后台决策与对账循环；
2. 对账恢复 ``POST /api/v1/bots/{bot_id}/reconcile``（``resume_if_clean=true``）：对账一致后把
   PAUSED / CIRCUIT_BROKEN 的机器人恢复运行。

第 2 个入口原来只把状态改成 RUNNING：
- **不重新创建后台循环**。暂停 / 熔断时循环已经退出，恢复后界面显示"运行中"，实际既不决策也不对账；
- **不做启动前检查**。实盘部署安全预检、交易凭证注入、同账户冲突检查都被绕过；
- 不写自动调度记录，重启后也不会恢复。

本模块把"启动"收敛成一个函数，两个入口共用，保证行为一致：

1. MAINNET：部署安全预检（弱口令 / 占位密钥一律拒绝，无豁免开关，M4）；
2. TESTNET / MAINNET：从金库注入精确匹配的交易凭证（失败即关闭）；
3. 同一交易所账户的同一标的只允许一个机器人运行（E7）；
4. 内存状态改为 RUNNING；
5. 创建后台决策与对账循环（已有决策循环但对账循环已退出时，只补建对账循环）；
6. 数据库状态改为 RUNNING，并写入自动调度记录（服务重启后据此恢复）。

任一检查不通过抛出 ``StartRefused``（带 HTTP 状态码与提示，提示文字与启动接口原来的一致），
此时机器人状态不变、不会创建循环。

依赖由调用方传入（bot_manager / scheduler / repo），便于测试替换；本模块不 import ``heimdall.api.server``。

运行日志（logger ``heimdall.api.server.bot_runtime``）
---------------------------------------------------
- INFO：启动成功（入口原因、模式、决策间隔）；
- WARN：启动被拒绝（原因）。
"""

import logging
from typing import Any

from heimdall.models.schemas import ExecutionMode
from heimdall.engine.schedule_state import save_schedule
from heimdall.utils.security_preflight import InsecureDeploymentError, assert_mainnet_ready

logger = logging.getLogger("heimdall.api.server.bot_runtime")


class StartRefused(Exception):
    """启动前检查未通过（接口层转换为 HTTPException）"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def start_bot_runtime(
    bot: Any,
    *,
    interval_seconds: int,
    reason: str,
    bot_manager: Any,
    scheduler: Any,
    repo: Any,
) -> None:
    """
    做齐启动前检查后让机器人开始运行（见模块说明）。

    :param interval_seconds: 后台决策间隔（秒）
    :param reason: 写入调度记录与日志的原因，例如 "钱包 0x… 启动"
    :raises StartRefused: 任一检查未通过
    """
    bot_id = bot.bot_id
    try:
        await _check_and_start(bot, interval_seconds, bot_manager, scheduler)
    except StartRefused as refused:
        logger.warning("[启动运行] %s 被拒绝（%s）: %s", bot_id, reason, refused.detail)
        raise

    await repo.update_agent_status(bot_id, "RUNNING")
    await save_schedule(bot, repo, enabled=True, interval_seconds=interval_seconds, reason=reason)
    logger.info(
        "[启动运行] %s 已开始运行（%s）| 模式=%s | 决策间隔=%d 秒",
        bot_id, reason, getattr(bot.execution_mode, "value", bot.execution_mode), interval_seconds,
    )


async def _check_and_start(bot: Any, interval_seconds: int, bot_manager: Any, scheduler: Any) -> None:
    # 1. M4：MAINNET 实盘启动前强制通过部署安全预检（弱口令 / 占位密钥一律拒绝，无豁免开关）
    if bot.execution_mode == ExecutionMode.MAINNET:
        try:
            assert_mainnet_ready()
        except InsecureDeploymentError as err:
            raise StartRefused(400, f"启动被拒绝：{err}")

    # 2. TESTNET / MAINNET 必须拥有精确匹配的有效凭证
    if bot.execution_mode != ExecutionMode.LOCAL_SIM:
        hydrated = await bot.hydrate_exchange_credentials_from_vault()
        if not hydrated:
            raise StartRefused(
                400,
                f"启动被拒绝：未找到与 {bot.exchange.value}/{bot.market_type.value}/{bot.execution_mode.value} "
                "精确匹配的有效活动凭证",
            )

    # 3. E7：同一交易所账户的同一标的只允许一个机器人运行（单向持仓下交易所会合并多个机器人的仓位）
    conflict = await bot_manager.check_account_conflict(bot)
    if conflict:
        raise StartRefused(
            409,
            f"启动被拒绝：机器人 '{conflict}' 正在同一账户的 {bot.symbol} 上交易（或仍有持仓/挂单）。"
            "同一账户同一标的只允许一个机器人运行，请先停止该机器人并平仓、撤单",
        )

    # 4. 更新内存状态（再做一次同步冲突检查，冲突时抛 ValueError）
    try:
        bot_manager.start_bot(bot.bot_id)
    except ValueError as err:
        raise StartRefused(409, f"启动被拒绝：{err}")

    # 5. 启动后台决策与对账循环
    await scheduler.start_bot_loop(bot, interval_seconds=interval_seconds)
