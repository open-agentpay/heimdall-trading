"""
凭证热重载服务（Credential Reloader）

背景（2026-11-22 修复）
----------------------
用户在密钥金库里切换 / 新建 / 删除 LLM 密钥后，运行中的机器人应立即改用新的激活密钥。
此前存在两个问题：

1. 激活接口（``POST /vault/keys/{id}/activate``）里的热重载代码导入了不存在的
   ``heimdall.engine.bot_manager.bot_manager``，又引用了不存在的 ``active_bots``，导入失败被 DEBUG 日志吞掉，
   **热重载从未执行**。而 ``LLMService.ensure_credentials()`` 在已启用时直接返回缓存，
   所以机器人会一直用旧密钥（例如从 DeepSeek 切到 Claude 后仍在调 DeepSeek），直到服务重启；
2. 编辑接口的热重载只刷新了 ``bot.llm_service``。配置了 ``HEIMDALL_LLM_MODEL_FAST / DEBATE / JUDGE``
   时，多 Agent 流水线会为分析师 / 辩论 / 裁判各自创建独立的 LLMService，这些实例不会被刷新；
   且任一机器人刷新出错会中断后续机器人，错误只记 DEBUG。

本服务统一处理：

- ``reload_llm``     ：找出钱包名下每个机器人能触达的 **全部** LLMService（按对象去重），逐个强制重载；
                       单个失败只影响它自己；
- ``reload_exchange``：钱包名下、交易所与密钥服务商一致的机器人，强制重新注入交易所凭证
                       （与此前编辑接口的行为一致，只在编辑当前激活的交易所密钥时调用）。

多租户：只刷新 ``owner_wallet`` 等于调用者钱包的机器人；系统公共机器人（owner=system）不受用户密钥变化影响。

已知限制：``LLMService.ensure_credentials(force_refresh=True)`` 会先清空内存密钥再读库，
读库这一小段时间内并发发起的 LLM 调用会自行重新加载（``call_llm`` 发现未启用时会调用 ensure_credentials），
最坏情况是重复读一次库，不会用到错误的密钥。

运行日志（logger ``heimdall.engine.bot_manager.fleet.credentials``）
------------------------------------------------------------------
- INFO ：每次热重载的汇总（原因、钱包、机器人数、LLM 服务数、刷新后已启用 / 无可用密钥 / 失败数量）；
- WARN ：单个服务或机器人刷新失败；刷新后某机器人没有可用 LLM 密钥（多 Agent 策略将走本地算法兜底）；
- DEBUG：钱包名下没有机器人。
日志中只出现钱包、机器人编号、模型名，不输出任何密钥内容。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from heimdall.engine.fleet.base import FleetHost, FleetService, get_fleet_logger, normalize_wallet, owner_of

logger = get_fleet_logger("credentials")

#: 单个 LLMService 重载的超时（秒）：只读一次数据库，正常远小于该值
LLM_RELOAD_TIMEOUT_SECONDS = 10.0
#: 单个机器人重新注入交易所凭证的超时（秒）
EXCHANGE_RELOAD_TIMEOUT_SECONDS = 15.0


@dataclass
class ReloadSummary:
    """一次热重载的结果（接口层只用于日志，不改变接口返回格式）"""

    kind: str                                   # "llm" / "exchange"
    wallet: str
    reason: str
    bots: List[str] = field(default_factory=list)
    services: int = 0                           # 刷新的 LLMService 实例数（exchange 为机器人数）
    enabled: int = 0                            # 刷新后可用的数量
    unavailable: int = 0                        # 刷新成功但没有可用密钥的数量
    failed: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "wallet": self.wallet, "reason": self.reason, "bots": list(self.bots),
            "services": self.services, "enabled": self.enabled, "unavailable": self.unavailable,
            "failed": list(self.failed),
        }


def _error_text(exc: BaseException) -> str:
    """异常的简短描述（超时等无消息的异常也能看出类型）"""
    msg = str(exc)
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def collect_llm_services(bot: Any) -> List[Any]:
    """
    找出机器人能触达的全部 LLMService（按对象去重，保持发现顺序）：
    bot.llm_service、bot.pipeline 与 bot.strategy.pipeline 的 llm / llm_router.default_service / llm_router.role_services。
    只要对象有 ``ensure_credentials`` 方法就视为 LLM 服务（便于测试替身）。
    """
    candidates: List[Any] = [getattr(bot, "llm_service", None)]
    pipelines = [getattr(bot, "pipeline", None), getattr(getattr(bot, "strategy", None), "pipeline", None)]
    for pipe in pipelines:
        if pipe is None:
            continue
        candidates.append(getattr(pipe, "llm", None))
        router = getattr(pipe, "llm_router", None)
        if router is not None:
            candidates.append(getattr(router, "default_service", None))
            candidates.extend((getattr(router, "role_services", None) or {}).values())
    seen: set = set()
    out: List[Any] = []
    for svc in candidates:
        if svc is None or not hasattr(svc, "ensure_credentials") or id(svc) in seen:
            continue
        seen.add(id(svc))
        out.append(svc)
    return out


class CredentialReloader(FleetService):
    """按钱包热重载运行中机器人的 LLM / 交易所凭证"""

    def __init__(self, host: FleetHost):
        super().__init__(host, logger)
        self.llm_timeout_seconds: float = LLM_RELOAD_TIMEOUT_SECONDS
        self.exchange_timeout_seconds: float = EXCHANGE_RELOAD_TIMEOUT_SECONDS

    def _bots_of(self, wallet: str) -> List[Any]:
        return [b for b in self.bots() if owner_of(b) == wallet]

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    async def reload_llm(self, wallet: Optional[str], reason: str = "") -> ReloadSummary:
        """强制重载钱包名下全部机器人的全部 LLMService；不抛出异常"""
        target = normalize_wallet(wallet) or ""
        summary = ReloadSummary(kind="llm", wallet=target, reason=reason)
        bots = self._bots_of(target) if target else []
        if not bots:
            self.log.debug("[凭证热重载] 钱包 %s 名下没有机器人，无需刷新 LLM 凭证（原因=%s）", target or "-", reason or "-")
            return summary

        for bot in bots:
            summary.bots.append(bot.bot_id)
            services = collect_llm_services(bot)
            bot_enabled = 0
            for svc in services:
                summary.services += 1
                try:
                    ok = await asyncio.wait_for(
                        svc.ensure_credentials(force_refresh=True, owner_wallet=target),
                        timeout=self.llm_timeout_seconds,
                    )
                except Exception as exc:
                    summary.failed.append({"bot_id": bot.bot_id, "error": _error_text(exc)})
                    self.log.warning("[凭证热重载] 机器人 %s 的 LLM 服务刷新失败: %s", bot.bot_id, _error_text(exc))
                    continue
                if ok:
                    summary.enabled += 1
                    bot_enabled += 1
                else:
                    summary.unavailable += 1
            if services and bot_enabled == 0 and not any(f["bot_id"] == bot.bot_id for f in summary.failed):
                self.log.warning(
                    "[凭证热重载] 机器人 %s 刷新后没有可用的 LLM 密钥，多 Agent 策略将走本地算法兜底", bot.bot_id,
                )

        self.log.info(
            "[凭证热重载] LLM | 原因=%s | 钱包=%s | 机器人 %d 个 | LLM 服务 %d 个 | 已启用 %d | 无可用密钥 %d | 失败 %d",
            reason or "-", target, len(summary.bots), summary.services, summary.enabled,
            summary.unavailable, len(summary.failed),
        )
        return summary

    # ------------------------------------------------------------------
    # 交易所
    # ------------------------------------------------------------------

    async def reload_exchange(self, wallet: Optional[str], provider: str, reason: str = "") -> ReloadSummary:
        """
        钱包名下、交易所与 provider 一致的机器人强制重新注入交易所凭证；不抛出异常。
        只用于"编辑当前激活的交易所密钥"（同一账户换新的 API 密钥），与此前行为一致。
        """
        target = normalize_wallet(wallet) or ""
        prov = (provider or "").strip().lower()
        summary = ReloadSummary(kind="exchange", wallet=target, reason=reason)
        bots = [b for b in self._bots_of(target) if getattr(getattr(b, "exchange", None), "value", "").lower() == prov] if target else []
        if not bots:
            self.log.debug("[凭证热重载] 钱包 %s 名下没有 %s 机器人，无需刷新交易所凭证", target or "-", prov or "-")
            return summary

        for bot in bots:
            summary.bots.append(bot.bot_id)
            summary.services += 1
            try:
                ok = await asyncio.wait_for(
                    bot.hydrate_exchange_credentials_from_vault(force_refresh=True),
                    timeout=self.exchange_timeout_seconds,
                )
            except Exception as exc:
                summary.failed.append({"bot_id": bot.bot_id, "error": _error_text(exc)})
                self.log.warning("[凭证热重载] 机器人 %s 重新注入交易所凭证失败: %s", bot.bot_id, _error_text(exc))
                continue
            if ok:
                summary.enabled += 1
            else:
                summary.unavailable += 1
                self.log.warning("[凭证热重载] 机器人 %s 没有找到精确匹配的交易所凭证，下一轮决策将失败即关闭", bot.bot_id)

        self.log.info(
            "[凭证热重载] 交易所 %s | 原因=%s | 钱包=%s | 机器人 %d 个 | 成功 %d | 无匹配凭证 %d | 失败 %d",
            prov, reason or "-", target, len(summary.bots), summary.enabled, summary.unavailable, len(summary.failed),
        )
        return summary
