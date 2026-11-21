"""
================================================================================
Heimdall 交易平台 - Hyperliquid DEX 实盘执行与账户查询适配器 (Hyperliquid Adapter)
================================================================================

【核心能力】：
1. EIP-712 Phantom Agent L1 离线签名：
   - 按照 Hyperliquid 官方 L1 协议规范，将 `action`（msgpack 序列化）+ 8 字节大端 `nonce` + 可选 `vaultAddress`
     通过 `keccak256` 压缩为 32 字节 `connectionId`；
   - 构造 `Exchange` 域（`chainId=1337`）的 `Agent` 类型化消息（主网 `source="a"` / 测试网 `source="b"`），
     在本地进程内通过 `eth_account` 离线签名，确保钱包私钥永不出域；
2. 真实 `/info` 账户与元数据查询：
   - 接入 `clearinghouseState` 实时拉取账户净值、可用余额、已占保证金、总未实现盈亏及逐币种持仓明细；
   - 接入 `meta` 缓存币种到 `asset_index` 的映射；
   - 接入 `openOrders` 与 `orderStatus` 支持批量撤单与 OMS `UNKNOWN` 订单状态对账恢复；
3. 真实 `/exchange` 签名交易执行：
   - 集成 `OrderNormalizer`（`Decimal` 精度截断），杜绝浮点精度拒单；
   - 支持普通限价/市价下单、交易所原生 `reduceOnly=True` 条件止损/止盈单（`trigger.tpsl`）、单笔与批量撤单；
   - 未配置私钥时自动降级为 Dry-Run 演练模式并输出明确中文告警，避免误触实盘资金。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

import httpx
import msgpack
from eth_account import Account
from eth_utils import keccak

from heimdall.execution.base import BaseExecutionAdapter, TradeExecutionResult
from heimdall.execution.precision import (
    InstrumentRegistry,
    OrderNormalizer,
    _normalize_symbol,
)
from heimdall.models.schemas import (
    ExchangeType,
    OrderSide,
    PositionSide,
    TradeProposal,
)

logger = logging.getLogger(__name__)

# Hyperliquid L1 EIP-712 固定签名域（主网与测试网均使用 chainId=1337）
_HL_EIP712_DOMAIN: Dict[str, Any] = {
    "chainId": 1337,
    "name": "Exchange",
    "verifyingContract": "0x0000000000000000000000000000000000000000",
    "version": "1",
}

# Hyperliquid L1 Phantom Agent EIP-712 类型定义
_HL_EIP712_TYPES: Dict[str, Any] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Agent": [
        {"name": "source", "type": "string"},
        {"name": "connectionId", "type": "bytes32"},
    ],
}


def _float_to_wire(val: Union[float, int, Decimal, str]) -> str:
    """
    将数值格式化为 Hyperliquid L1 协议要求的标准无尾零十进制字符串。
    例如：3125.00 -> "3125"，0.3200 -> "0.32"，避免科学计数法导致签名哈希与服务端校验不一致。
    """
    d = Decimal(str(val)).normalize()
    if d == 0:
        return "0"
    # 使用固定点格式避免出现 '1E+3' 等科学计数法
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def compute_l1_action_hash(
    action: Dict[str, Any],
    nonce: int,
    vault_address: Optional[str] = None,
) -> bytes:
    """
    计算 Hyperliquid L1 动作哈希（即 Phantom Agent 的 `connectionId`）：
    keccak256(msgpack(action) || uint64_be(nonce) || (0x00 | 0x01 || vault_bytes))

    :param action: Hyperliquid `/exchange` 请求中的 action 字典
    :param nonce: 毫秒级递增防重放时间戳
    :param vault_address: 可选金库/子账户地址（0x 开头的 20 字节十六进制地址）
    :return: 32 字节 keccak256 哈希
    """
    packed = bytearray(msgpack.packb(action))
    packed.extend(int(nonce).to_bytes(8, "big"))
    if vault_address:
        clean_vault = vault_address.lower().removeprefix("0x")
        packed.append(1)
        packed.extend(bytes.fromhex(clean_vault))
    else:
        packed.append(0)
    return bytes(keccak(bytes(packed)))


def sign_l1_action(
    private_key: str,
    action: Dict[str, Any],
    nonce: int,
    is_testnet: bool = True,
    vault_address: Optional[str] = None,
) -> Dict[str, Any]:
    """
    使用本地私钥对 Hyperliquid L1 Action 执行 EIP-712 Phantom Agent 离线签名。

    :param private_key: 钱包私钥（支持带或不带 0x 前缀）
    :param action: 待签名的 L1 action 字典
    :param nonce: 毫秒级时间戳 nonce
    :param is_testnet: 是否为测试网（测试网 source='b'，主网 source='a'）
    :param vault_address: 可选金库地址
    :return: Hyperliquid `/exchange` 所需的 {"r": "0x...", "s": "0x...", "v": 27|28} 签名字典
    """
    connection_id = compute_l1_action_hash(action, nonce=nonce, vault_address=vault_address)
    typed_data: Dict[str, Any] = {
        "domain": _HL_EIP712_DOMAIN,
        "types": _HL_EIP712_TYPES,
        "primaryType": "Agent",
        "message": {
            "source": "b" if is_testnet else "a",
            "connectionId": connection_id,
        },
    }
    signed = Account.sign_typed_data(private_key, full_message=typed_data)
    return {
        "r": hex(signed.r),
        "s": hex(signed.s),
        "v": int(signed.v),
    }


def _map_hl_order_status(raw_status: str) -> str:
    """将 Hyperliquid `orderStatus` 返回的状态字符串映射为 Heimdall OMS 标准状态"""
    s = (raw_status or "").lower().strip()
    if s == "filled":
        return "FILLED"
    if s == "open":
        return "OPEN"
    if "cancel" in s:
        return "CANCELED"
    if "reject" in s:
        return "REJECTED"
    if s == "triggered":
        return "SUBMITTED"
    return s.upper() or "UNKNOWN"


class HyperliquidAdapter(BaseExecutionAdapter):
    """
    Hyperliquid 交易所实盘与测试网执行适配器。
    实现完整 EIP-712 离线签名下单、条件止损止盈、真实账户净值/持仓查询、批量撤单与订单对账。
    """

    def __init__(
        self,
        wallet_address: str = "",
        private_key: str = "",
        testnet: bool = True,
        base_url: Optional[str] = None,
        vault_address: Optional[str] = None,
        instrument_registry: Optional[InstrumentRegistry] = None,
        timeout_s: float = 10.0,
    ):
        """
        初始化 Hyperliquid 实盘/测试网适配器。

        :param wallet_address: Arbitrum / Hyperliquid L1 钱包地址（若留空但传入私钥，则自动从私钥派生）
        :param private_key: 钱包私钥（仅用于本地离线 EIP-712 签名，私钥不出域）
        :param testnet: 是否连接 Hyperliquid Testnet 测试网（默认 True，安全第一）
        :param base_url: 自定义 API 网关地址
        :param vault_address: 可选 Hyperliquid Vault 金库地址
        :param instrument_registry: 标的精度注册表（未传入时自动初始化内置注册表）
        :param timeout_s: HTTP 请求超时时间（秒）
        """
        self.private_key = (private_key or "").strip()
        if wallet_address and wallet_address.strip():
            self.wallet_address = wallet_address.strip()
        elif self.private_key:
            # 未显式传入钱包地址时，根据私钥在本地安全派生对应地址
            self.wallet_address = Account.from_key(self.private_key).address
        else:
            self.wallet_address = ""

        self.testnet = testnet
        self.vault_address = vault_address.strip() if vault_address else None
        self.base_url = (
            base_url
            or ("https://api.hyperliquid-testnet.xyz" if testnet else "https://api.hyperliquid.xyz")
        ).rstrip("/")
        self.registry = instrument_registry or InstrumentRegistry()
        self.timeout_s = timeout_s
        # 币种名称 -> Hyperliquid L1 资产索引（universe 下标）缓存
        self._asset_index_cache: Dict[str, int] = {}

        masked_addr = (
            f"{self.wallet_address[:6]}...{self.wallet_address[-4:]}"
            if len(self.wallet_address) >= 12
            else (self.wallet_address or "未配置")
        )
        logger.info(
            "[Hyperliquid 适配器] 初始化完成 | 网络=%s | 节点=%s | 钱包=%s | 签名模式=%s",
            "TESTNET" if self.testnet else "MAINNET",
            self.base_url,
            masked_addr,
            "实盘EIP-712签名" if self.private_key else "Dry-Run演练(无私钥)",
        )

    async def _post_info(self, payload: Dict[str, Any]) -> Any:
        """向 Hyperliquid `/info` 只读查询端点发起 POST 请求"""
        url = f"{self.base_url}/info"
        req_type = payload.get("type", "unknown")
        logger.debug("[Hyperliquid /info 请求] type=%s | url=%s", req_type, url)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def _post_exchange(
        self,
        action: Dict[str, Any],
        nonce: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        对 L1 Action 执行本地 EIP-712 离线签名并提交至 Hyperliquid `/exchange` 交易端点。
        """
        if not self.private_key:
            raise RuntimeError("Hyperliquid 实盘交易端点需要配置钱包私钥，当前处于无私钥状态")

        nonce_ms = int(nonce if nonce is not None else time.time() * 1000)
        signature = sign_l1_action(
            private_key=self.private_key,
            action=action,
            nonce=nonce_ms,
            is_testnet=self.testnet,
            vault_address=self.vault_address,
        )
        body: Dict[str, Any] = {
            "action": action,
            "nonce": nonce_ms,
            "signature": signature,
        }
        if self.vault_address:
            body["vaultAddress"] = self.vault_address

        url = f"{self.base_url}/exchange"
        logger.info(
            "[Hyperliquid /exchange 提交] action_type=%s | nonce=%d | 网络=%s",
            action.get("type"),
            nonce_ms,
            "TESTNET" if self.testnet else "MAINNET",
        )
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            logger.debug("[Hyperliquid /exchange 响应] status=%s", data.get("status"))
            return data

    async def get_asset_index(self, symbol: str) -> int:
        """
        获取标的在 Hyperliquid `universe` 中的整数资产索引（如 BTC=0, ETH=1）。
        首次调用自动请求 `/info` (`type=meta`) 并缓存在内存中。
        """
        coin = _normalize_symbol(symbol)
        if coin in self._asset_index_cache:
            return self._asset_index_cache[coin]

        meta = await self._post_info({"type": "meta"})
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        for idx, item in enumerate(universe):
            name = str(item.get("name", "")).upper().strip()
            if name:
                self._asset_index_cache[name] = idx

        logger.info(
            "[Hyperliquid 元数据缓存] 已从 /info(meta) 加载 %d 个永续合约资产索引",
            len(self._asset_index_cache),
        )
        if coin not in self._asset_index_cache:
            logger.error("[Hyperliquid 元数据错误] 标的 %s 不在交易所 universe 列表中", coin)
            raise ValueError(f"Hyperliquid 交易所未找到标的资产索引: {coin}")
        return self._asset_index_cache[coin]

    async def get_clearinghouse_state(self) -> Dict[str, Any]:
        """调用 `/info` (`clearinghouseState`) 获取当前钱包的完整清算与保证金状态"""
        if not self.wallet_address:
            logger.warning("[Hyperliquid 账户查询] 未配置 wallet_address，无法查询清算状态")
            return {}
        data = await self._post_info({
            "type": "clearinghouseState",
            "user": self.wallet_address,
        })
        return data if isinstance(data, dict) else {}

    async def get_account_balance(self) -> Dict[str, float]:
        """
        获取 Hyperliquid 真实账户净值、可用余额、已占保证金与总未实现盈亏。
        直接解析 `/info` 的 `clearinghouseState` 响应，彻底取代历史写死桩数据。
        """
        state = await self.get_clearinghouse_state()
        margin_summary = state.get("marginSummary", {}) or {}

        total_equity = float(margin_summary.get("accountValue", 0.0) or 0.0)
        margin_used = float(margin_summary.get("totalMarginUsed", 0.0) or 0.0)
        withdrawable = float(state.get("withdrawable", max(0.0, total_equity - margin_used)) or 0.0)

        # 汇总各持仓的未实现盈亏
        unrealized_pnl = 0.0
        for item in state.get("assetPositions", []) or []:
            pos = item.get("position", {}) if isinstance(item, dict) else {}
            try:
                unrealized_pnl += float(pos.get("unrealizedPnl", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue

        result = {
            "total_equity": round(total_equity, 4),
            "cash_balance": round(withdrawable, 4),
            "margin_used": round(margin_used, 4),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "realized_pnl_cum": 0.0,
        }
        logger.info(
            "[Hyperliquid 账户余额] 净值=$%.2f | 可用=$%.2f | 已占保证金=$%.2f | 未实现盈亏=$%.2f",
            result["total_equity"],
            result["cash_balance"],
            result["margin_used"],
            result["unrealized_pnl"],
        )
        return result

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        获取 Hyperliquid 当前存续的活动持仓列表。
        自动过滤掉仓位为 0 的空记录，并标准化为统一持仓字典结构。
        """
        state = await self.get_clearinghouse_state()
        positions: List[Dict[str, Any]] = []

        for item in state.get("assetPositions", []) or []:
            pos = item.get("position", {}) if isinstance(item, dict) else {}
            try:
                szi = float(pos.get("szi", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if abs(szi) <= 1e-12:
                continue

            coin = str(pos.get("coin", "")).upper().strip()
            side = "LONG" if szi > 0 else "SHORT"
            qty = abs(szi)
            entry_px = float(pos.get("entryPx", 0.0) or 0.0)
            margin_used = float(pos.get("marginUsed", 0.0) or 0.0)
            upnl = float(pos.get("unrealizedPnl", 0.0) or 0.0)
            liq_raw = pos.get("liquidationPx")
            liq_px = float(liq_raw) if liq_raw not in (None, "") else 0.0
            lev_info = pos.get("leverage", {}) or {}
            leverage = float(lev_info.get("value", 1.0) or 1.0)

            positions.append({
                "symbol": coin,
                "side": side,
                "quantity": qty,
                "size": qty,
                "entry_price": entry_px,
                "margin_used": margin_used,
                "unrealized_pnl": upnl,
                "liquidation_price": liq_px,
                "leverage": leverage,
            })

        logger.info("[Hyperliquid 持仓查询] 当前活动持仓数=%d", len(positions))
        return positions

    async def execute_proposal(self, proposal: TradeProposal) -> TradeExecutionResult:
        """
        执行经风控核准的交易提案：
        1. 通过 `OrderNormalizer` 按 Hyperliquid 标的精度截断委托价与数量；
        2. 若已配置私钥，则构造 L1 订单并离线 EIP-712 签名提交 `/exchange`；
        3. 若未配置私钥，则安全降级为 Dry-Run 演练回报并输出中文告警。
        """
        action = proposal.trade_action
        coin = _normalize_symbol(action.symbol)
        spec = self.registry.get_spec("HYPERLIQUID", coin)
        norm = OrderNormalizer.normalize_proposal(proposal, spec)

        is_buy = action.order_side == OrderSide.BUY
        trade_id = f"hl_trd_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{proposal.proposal_id[-6:]}"

        # 无私钥时安全降级为演练模式，防止误报错误或未授权实盘发单
        if not self.private_key:
            est_fee = float(norm.notional_usd) * 0.00035
            logger.warning(
                "[Hyperliquid 演练下单] 未配置私钥，跳过 /exchange 实盘发单 | symbol=%s 方向=%s 价格=%s 数量=%s",
                coin,
                "BUY" if is_buy else "SELL",
                norm.price,
                norm.quantity,
            )
            return TradeExecutionResult(
                trade_id=trade_id,
                order_id=f"hl_dry_{proposal.proposal_id[-8:]}",
                proposal_id=proposal.proposal_id,
                exchange=ExchangeType.HYPERLIQUID,
                symbol=coin,
                order_side=action.order_side,
                position_side=action.position_side,
                executed_price=float(norm.price),
                executed_quantity=float(norm.quantity),
                executed_size_usd=float(norm.notional_usd),
                fee_usd=round(est_fee, 4),
                realized_pnl=0.0,
                executed_at=datetime.now(timezone.utc),
                raw_response={"status": "dry_run", "order_type": action.order_type},
            )

        asset_idx = await self.get_asset_index(coin)
        # 市价单在 Hyperliquid L1 中对应 IOC（Immediate-Or-Cancel），限价单默认 GTC
        tif = "Ioc" if (action.order_type or "").upper() == "MARKET" else "Gtc"
        hl_action: Dict[str, Any] = {
            "type": "order",
            "orders": [
                {
                    "a": asset_idx,
                    "b": is_buy,
                    "p": _float_to_wire(norm.price),
                    "s": _float_to_wire(norm.quantity),
                    "r": False,
                    "t": {"limit": {"tif": tif}},
                }
            ],
            "grouping": "na",
        }

        logger.info(
            "[Hyperliquid 实盘下单] symbol=%s(idx=%d) | 方向=%s | 委托价=%s | 数量=%s | TIF=%s | 杠杆=%.1fx",
            coin,
            asset_idx,
            "BUY" if is_buy else "SELL",
            norm.price,
            norm.quantity,
            tif,
            action.leverage,
        )

        raw_resp = await self._post_exchange(hl_action)
        if raw_resp.get("status") != "ok":
            logger.error("[Hyperliquid 下单被拒] 响应异常: %s", raw_resp)
            raise RuntimeError(f"Hyperliquid 下单失败: {raw_resp}")

        statuses = (
            raw_resp.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        first_status = statuses[0] if statuses else {}
        if isinstance(first_status, dict) and "error" in first_status:
            logger.error("[Hyperliquid 撮合拒单] 标的=%s 原因=%s", coin, first_status["error"])
            raise RuntimeError(f"Hyperliquid 撮合引擎拒单: {first_status['error']}")

        # 解析即时成交 (filled) 或挂单排队 (resting) 回报
        order_id = f"hl_ord_{proposal.proposal_id[-8:]}"
        exec_price = float(norm.price)
        exec_qty = float(norm.quantity)
        if isinstance(first_status, dict) and "filled" in first_status:
            filled = first_status["filled"]
            order_id = str(filled.get("oid", order_id))
            exec_price = float(filled.get("avgPx", exec_price))
            exec_qty = float(filled.get("totalSz", exec_qty))
        elif isinstance(first_status, dict) and "resting" in first_status:
            resting = first_status["resting"]
            order_id = str(resting.get("oid", order_id))

        exec_size_usd = round(exec_price * exec_qty, 2)
        est_fee = round(exec_size_usd * 0.00035, 4)

        logger.info(
            "[Hyperliquid 下单回报] order_id=%s | symbol=%s | 成交价=%.4f | 成交量=%.4f | 名义额=$%.2f",
            order_id,
            coin,
            exec_price,
            exec_qty,
            exec_size_usd,
        )
        return TradeExecutionResult(
            trade_id=trade_id,
            order_id=order_id,
            proposal_id=proposal.proposal_id,
            exchange=ExchangeType.HYPERLIQUID,
            symbol=coin,
            order_side=action.order_side,
            position_side=action.position_side,
            executed_price=exec_price,
            executed_quantity=exec_qty,
            executed_size_usd=exec_size_usd,
            fee_usd=est_fee,
            realized_pnl=0.0,
            executed_at=datetime.now(timezone.utc),
            raw_response=raw_resp,
        )

    async def place_conditional_order(
        self,
        symbol: str,
        side: OrderSide,
        position_side: PositionSide,
        order_type: str,
        trigger_price: float,
        quantity: float,
        reduce_only: bool = True,
        working_type: str = "MARK_PRICE",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        向 Hyperliquid 挂载交易所原生条件止损/止盈单（强制支持 `reduceOnly=True` 与 `trigger.tpsl`）。
        即便本地策略进程崩溃，交易所撮合引擎仍会按触发价自动平仓保护本金。
        """
        coin = _normalize_symbol(symbol)
        spec = self.registry.get_spec("HYPERLIQUID", coin)
        q_trigger = OrderNormalizer.quantize_price(trigger_price, spec)
        q_qty = (Decimal(str(quantity)) / spec.step_size).to_integral_value() * spec.step_size
        if q_qty <= 0:
            q_qty = Decimal(str(quantity))

        tpsl = "tp" if "TAKE_PROFIT" in (order_type or "").upper() else "sl"
        is_buy = side == OrderSide.BUY

        if not self.private_key:
            logger.warning(
                "[Hyperliquid 演练条件单] 未配置私钥，返回本地演练回执 | symbol=%s tpsl=%s trigger=%s qty=%s",
                coin,
                tpsl,
                q_trigger,
                q_qty,
            )
            return await super().place_conditional_order(
                symbol=coin,
                side=side,
                position_side=position_side,
                order_type=order_type,
                trigger_price=float(q_trigger),
                quantity=float(q_qty),
                reduce_only=reduce_only,
                working_type=working_type,
                client_order_id=client_order_id,
            )

        asset_idx = await self.get_asset_index(coin)
        trigger_wire = _float_to_wire(q_trigger)
        hl_action: Dict[str, Any] = {
            "type": "order",
            "orders": [
                {
                    "a": asset_idx,
                    "b": is_buy,
                    "p": trigger_wire,
                    "s": _float_to_wire(q_qty),
                    "r": bool(reduce_only),
                    "t": {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": trigger_wire,
                            "tpsl": tpsl,
                        }
                    },
                }
            ],
            "grouping": "na",
        }

        logger.info(
            "[Hyperliquid 挂载条件单] symbol=%s(idx=%d) | 类型=%s(%s) | 触发价=%s | 数量=%s | reduceOnly=%s",
            coin,
            asset_idx,
            order_type,
            tpsl,
            trigger_wire,
            q_qty,
            reduce_only,
        )
        raw_resp = await self._post_exchange(hl_action)
        if raw_resp.get("status") != "ok":
            logger.error("[Hyperliquid 条件单挂单失败] 响应=%s", raw_resp)
            raise RuntimeError(f"Hyperliquid 条件单提交失败: {raw_resp}")

        statuses = raw_resp.get("response", {}).get("data", {}).get("statuses", [])
        first = statuses[0] if statuses else {}
        oid = client_order_id or f"hl_cond_{int(time.time() * 1000)}"
        if isinstance(first, dict):
            if "error" in first:
                raise RuntimeError(f"Hyperliquid 条件单被拒: {first['error']}")
            if "resting" in first and "oid" in first["resting"]:
                oid = str(first["resting"]["oid"])
            elif "filled" in first and "oid" in first["filled"]:
                oid = str(first["filled"]["oid"])

        return {
            "order_id": oid,
            "client_order_id": client_order_id or oid,
            "symbol": coin,
            "side": side.value,
            "position_side": position_side.value,
            "order_type": order_type,
            "trigger_price": float(q_trigger),
            "quantity": float(q_qty),
            "reduce_only": bool(reduce_only),
            "working_type": working_type,
            "status": "SUBMITTED",
            "raw_response": raw_resp,
        }

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> bool:
        """撤销指定订单 ID 的 Hyperliquid 活动挂单"""
        if not order_id:
            logger.warning("[Hyperliquid 单笔撤单] 缺少 order_id，跳过撤单请求")
            return False
        if not self.private_key:
            logger.warning("[Hyperliquid 演练撤单] 未配置私钥，跳过 /exchange 撤单 | oid=%s", order_id)
            return True

        coin = _normalize_symbol(symbol)
        asset_idx = await self.get_asset_index(coin)
        cancel_action = {
            "type": "cancel",
            "cancels": [{"a": asset_idx, "o": int(order_id)}],
        }
        resp = await self._post_exchange(cancel_action)
        ok = resp.get("status") == "ok"
        logger.info(
            "[Hyperliquid 单笔撤单] symbol=%s | oid=%s | 结果=%s",
            coin,
            order_id,
            "成功" if ok else f"失败({resp})",
        )
        return ok

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """
        查询 `/info` (`openOrders`) 获取当前所有活动挂单，
        并按标的过滤后通过 EIP-712 签名向 `/exchange` 批量下发 `cancel` 指令。
        """
        if not self.wallet_address:
            logger.warning("[Hyperliquid 批量撤单] 未配置 wallet_address，跳过撤单")
            return False

        open_orders = await self._post_info({
            "type": "openOrders",
            "user": self.wallet_address,
        })
        if not isinstance(open_orders, list):
            open_orders = []

        target_coin = _normalize_symbol(symbol) if symbol else None
        cancels: List[Dict[str, Any]] = []
        for o in open_orders:
            coin = _normalize_symbol(str(o.get("coin", "")))
            if target_coin and coin != target_coin:
                continue
            oid = o.get("oid")
            if oid is None:
                continue
            asset_idx = await self.get_asset_index(coin)
            cancels.append({"a": asset_idx, "o": int(oid)})

        if not cancels:
            logger.info("[Hyperliquid 批量撤单] 当前无匹配的活动挂单需要撤销 (symbol=%s)", target_coin or "ALL")
            return True

        if not self.private_key:
            logger.warning(
                "[Hyperliquid 演练批量撤单] 发现 %d 笔挂单，但未配置私钥，跳过实盘撤单",
                len(cancels),
            )
            return True

        resp = await self._post_exchange({"type": "cancel", "cancels": cancels})
        ok = resp.get("status") == "ok"
        logger.info(
            "[Hyperliquid 批量撤单完成] 标的=%s | 撤销笔数=%d | 状态=%s",
            target_coin or "ALL",
            len(cancels),
            "成功" if ok else f"异常({resp})",
        )
        return ok

    async def query_order(
        self,
        symbol: str,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        通过 `/info` (`orderStatus`) 查询指定订单的真实撮合状态，
        用于 OMS 网络超时后的 `UNKNOWN` 状态对账恢复。
        """
        if not self.wallet_address or not order_id:
            logger.warning(
                "[Hyperliquid 查单跳过] wallet_address 或 order_id 缺失 (oid=%s)",
                order_id,
            )
            return {
                "found": False,
                "status": "NOT_FOUND",
                "client_order_id": client_order_id,
                "order_id": order_id,
            }

        try:
            oid_int = int(order_id)
        except (TypeError, ValueError):
            logger.warning("[Hyperliquid 查单跳过] 非法数值型 order_id=%s", order_id)
            return {
                "found": False,
                "status": "NOT_FOUND",
                "client_order_id": client_order_id,
                "order_id": order_id,
            }

        resp = await self._post_info({
            "type": "orderStatus",
            "user": self.wallet_address,
            "oid": oid_int,
        })
        if not isinstance(resp, dict) or resp.get("status") == "unknownOid":
            logger.info("[Hyperliquid 查单结果] 未找到订单 oid=%s", order_id)
            return {
                "found": False,
                "status": "NOT_FOUND",
                "client_order_id": client_order_id,
                "order_id": str(order_id),
            }

        order_wrapper = resp.get("order", {}) or {}
        raw_status = str(order_wrapper.get("status", ""))
        mapped_status = _map_hl_order_status(raw_status)
        inner = order_wrapper.get("order", {}) or {}

        filled_qty = float(inner.get("sz", 0.0) or 0.0)
        limit_px = float(inner.get("limitPx", 0.0) or 0.0)

        logger.info(
            "[Hyperliquid 查单结果] symbol=%s | oid=%s | 原始状态=%s -> OMS状态=%s | 数量=%s | 价格=%s",
            _normalize_symbol(symbol),
            order_id,
            raw_status,
            mapped_status,
            filled_qty,
            limit_px,
        )
        return {
            "found": True,
            "status": mapped_status,
            "order_id": str(order_id),
            "client_order_id": client_order_id,
            "executed_quantity": filled_qty,
            "executed_price": limit_px,
            "raw_response": resp,
        }
