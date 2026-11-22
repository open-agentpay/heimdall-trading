#!/usr/bin/env python3
"""
================================================================================
币安 U 本位合约测试网验证脚本（对应审计报告第 10.5 节）
================================================================================

验证内容：
  0. 测试网地址：官方文档的新地址 demo-fapi.binance.com 与项目里写死的
     testnet.binancefuture.com 哪个可用、哪个接受你的 API Key
  1. 持仓模式（单向 / 双向）              —— E10 是否会触发
  2. 不传 newOrderRespType 的市价单回包    —— E1：是否只返回 ACK（executedQty=0）
  3. 查单结果与成交明细（手续费、已实现盈亏）—— E2 / E8
  4. 旧接口 /fapi/v1/order 下 STOP_MARKET  —— E4：是否返回 -4120
  5. Algo 接口 /fapi/v1/algoOrder 下止损单  —— E4 的修复方向是否可行
  6. /fapi/v1/openOrders、/fapi/v1/allOpenOrders 能否看到 / 撤掉 Algo 条件单
  7. newOrderRespType=RESULT 的平仓回包    —— 修复后能否直接拿到成交结果

安全措施：
  - 只允许连接测试网地址，检测到主网地址直接退出；
  - 默认只做只读检查；加 --trade 才会下单，且需要手动输入 YES 确认；
  - 只下一笔接近最小名义金额的市价单，结束时（包括出错时）自动撤销本脚本挂的
    条件单并平掉本脚本开的仓；
  - 标的上已有持仓或挂单时拒绝下单，避免干扰你现有的测试；
  - 密钥只从环境变量读取，日志与结果文件里只保留打码后的 API Key。

用法：
  export HEIMDALL_PROBE_API_KEY=你的测试网 API Key
  export HEIMDALL_PROBE_API_SECRET=你的测试网 Secret
  python3 scripts/binance_futures_testnet_probe.py              # 只读检查
  python3 scripts/binance_futures_testnet_probe.py --trade      # 下单验证
  python3 scripts/binance_futures_testnet_probe.py --trade --symbol BTCUSDT

结果写入 data/testnet_probe/probe_<时间>.json（data/ 已被 Git 忽略）。
只依赖 Python 标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NEW_TESTNET = "https://demo-fapi.binance.com"          # 币安官方文档当前的测试网地址
LEGACY_TESTNET = "https://testnet.binancefuture.com"   # 项目 execution/binance.py 中写死的地址
MAINNET_HOSTS = ("fapi.binance.com", "api.binance.com", "dapi.binance.com")
ROOT = Path(__file__).resolve().parent.parent

RESULT: Dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(), "steps": {}, "verdicts": {}}


# ------------------------------------------------------------------------------
# 基础工具
# ------------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def mask(s: str) -> str:
    return f"{s[:4]}****{s[-4:]}" if len(s) > 8 else "****"


class Client:
    def __init__(self, base: str, key: str, secret: str):
        host = urllib.parse.urlparse(base).hostname or ""
        if host in MAINNET_HOSTS:
            sys.exit(f"拒绝运行：{base} 是主网地址，本脚本只允许连接测试网")
        self.base = base.rstrip("/")
        self.key = key
        self.secret = secret
        self.time_offset_ms = 0

    def _send(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
              signed: bool = False, timeout: float = 8.0) -> Tuple[int, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            params.setdefault("recvWindow", 5000)
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query = f"{query}&signature={sig}"
            headers["X-MBX-APIKEY"] = self.key
        else:
            query = urllib.parse.urlencode(params)
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
                return resp.status, json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"raw": body[:500]}
        except Exception as e:  # 网络不可达、DNS 失败、超时
            return -1, {"error": f"{type(e).__name__}: {e}"}

    def get(self, path, params=None, signed=False):
        return self._send("GET", path, params, signed)

    def post(self, path, params=None):
        return self._send("POST", path, params, signed=True)

    def delete(self, path, params=None):
        return self._send("DELETE", path, params, signed=True)

    def sync_time(self) -> bool:
        st, data = self.get("/fapi/v1/time")
        if st == 200 and isinstance(data, dict) and "serverTime" in data:
            self.time_offset_ms = int(data["serverTime"]) - int(time.time() * 1000)
            return True
        return False


def record(step: str, **kw: Any) -> None:
    RESULT["steps"][step] = kw


def verdict(code: str, ok: Optional[bool], text: str) -> None:
    RESULT["verdicts"][code] = {"confirmed": ok, "detail": text}
    tag = {True: "✅ 确认", False: "❌ 不成立", None: "⚠️ 无法判断"}[ok]
    log(f"  [{code}] {tag}：{text}")


def q_up(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_UP) * step


def q_down(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def dec_str(x: Decimal) -> str:
    return format(x.normalize(), "f")


# ------------------------------------------------------------------------------
# 只读检查
# ------------------------------------------------------------------------------

def probe_hosts(key: str, secret: str) -> Optional[str]:
    """检查新旧两个测试网地址，返回接受该 API Key 的地址"""
    log("\n== 0. 测试网地址检查 ==")
    accepted = None
    host_info = {}
    for base in (NEW_TESTNET, LEGACY_TESTNET):
        c = Client(base, key, secret)
        ping_st, ping = c.get("/fapi/v1/ping")
        synced = c.sync_time() if ping_st == 200 else False
        acc_st, acc = c.get("/fapi/v2/balance", signed=True) if synced else (None, None)
        ok = acc_st == 200
        host_info[base] = {
            "ping_status": ping_st,
            "ping_error": ping if ping_st != 200 else None,
            "signed_status": acc_st,
            "signed_error": acc if (acc_st is not None and not ok) else None,
            "time_offset_ms": c.time_offset_ms if synced else None,
        }
        log(f"  {base}: ping={ping_st} | 签名请求={acc_st if acc_st is not None else '未执行'}"
            + ("" if ok or acc is None else f" | 返回={acc}"))
        if ok and accepted is None:
            accepted = base
    record("hosts", **host_info)
    legacy_ok = host_info[LEGACY_TESTNET]["signed_status"] == 200
    new_ok = host_info[NEW_TESTNET]["signed_status"] == 200
    if new_ok and not legacy_ok:
        verdict("BASE_URL", True, "API Key 只在新地址 demo-fapi.binance.com 可用，项目写死的旧地址需要更新")
    elif legacy_ok and not new_ok:
        verdict("BASE_URL", False, "API Key 只在旧地址可用，项目当前地址没问题")
    elif legacy_ok and new_ok:
        verdict("BASE_URL", False, "新旧地址都可用，旧地址暂时不影响，但建议跟随官方文档迁移")
    else:
        verdict("BASE_URL", None, "两个地址都没有通过签名请求，请检查 API Key、IP 白名单或网络")
    return accepted


def readonly_checks(c: Client, symbol: str) -> Dict[str, Any]:
    log(f"\n== 1. 账户与标的状态（{c.base}）==")
    st, mode = c.get("/fapi/v1/positionSide/dual", signed=True)
    dual = bool(mode.get("dualSidePosition")) if st == 200 and isinstance(mode, dict) else None
    log(f"  持仓模式: {'双向持仓' if dual else '单向持仓' if dual is False else f'读取失败 {mode}'}")
    if dual is True:
        verdict("E10", True, "账户为双向持仓：项目里平多仓时把 position_side 改成 SHORT，会变成开空，而不是平多")
    elif dual is False:
        verdict("E10", False, "账户为单向持仓，E10 当前不会触发（切换为双向持仓后会触发）")

    _, bal = c.get("/fapi/v2/balance", signed=True)
    usdt = next((b for b in (bal or []) if isinstance(b, dict) and b.get("asset") == "USDT"), {})
    log(f"  USDT 余额: {usdt.get('balance')} | 可用: {usdt.get('availableBalance')}")

    _, pos = c.get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    pos_amt = sum(Decimal(str(p.get("positionAmt") or "0")) for p in (pos or []) if isinstance(p, dict))
    _, all_pos = c.get("/fapi/v2/positionRisk", signed=True)
    nonzero = [p.get("symbol") for p in (all_pos or []) if isinstance(p, dict) and abs(float(p.get("positionAmt") or 0)) > 0]
    log(f"  {symbol} 当前持仓数量: {pos_amt} | 账户内有持仓的标的: {nonzero or '无'}")

    _, oo = c.get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
    algo_st, ao = c.get("/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
    algo_list = ao if isinstance(ao, list) else (ao.get("orders") if isinstance(ao, dict) and isinstance(ao.get("orders"), list) else None)
    log(f"  {symbol} 普通挂单: {len(oo) if isinstance(oo, list) else oo} | Algo 条件单接口: HTTP {algo_st}, "
        f"{len(algo_list) if algo_list is not None else ao}")

    _, info = c.get("/fapi/v1/exchangeInfo")
    sym_info = next((s for s in (info or {}).get("symbols", []) if s.get("symbol") == symbol), None)
    if not sym_info:
        sys.exit(f"测试网 exchangeInfo 中找不到 {symbol}")
    f = {x["filterType"]: x for x in sym_info["filters"]}
    lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
    spec = {
        "step": Decimal(lot["stepSize"]) if Decimal(lot["stepSize"]) > 0 else Decimal(f["LOT_SIZE"]["stepSize"]),
        "min_qty": max(Decimal(lot["minQty"]), Decimal(f["LOT_SIZE"]["minQty"])),
        "tick": Decimal(f["PRICE_FILTER"]["tickSize"]),
        "min_notional": Decimal(str(f.get("MIN_NOTIONAL", {}).get("notional", "5"))),
    }
    _, prem = c.get("/fapi/v1/premiumIndex", {"symbol": symbol})
    mark = Decimal(str(prem.get("markPrice"))) if isinstance(prem, dict) and prem.get("markPrice") else None
    log(f"  标记价格: {mark} | 最小名义金额: {spec['min_notional']} | 数量步长: {spec['step']} | 价格步长: {spec['tick']}")

    state = {
        "dual_side": dual, "usdt": usdt, "symbol_position_amt": str(pos_amt), "symbols_with_position": nonzero,
        "open_orders": len(oo) if isinstance(oo, list) else oo,
        "open_algo_orders_status": algo_st, "open_algo_orders": algo_list if algo_list is not None else ao,
        "spec": {k: str(v) for k, v in spec.items()}, "mark_price": str(mark) if mark else None,
    }
    record("readonly", **state)
    state.update(_spec=spec, _mark=mark, _pos_amt=pos_amt,
                 _open_count=(len(oo) if isinstance(oo, list) else 0) + (len(algo_list) if algo_list else 0))
    return state


# ------------------------------------------------------------------------------
# 下单验证
# ------------------------------------------------------------------------------

def trade_checks(c: Client, symbol: str, st: Dict[str, Any]) -> None:
    spec, mark, dual = st["_spec"], st["_mark"], st["dual_side"]
    if mark is None or dual is None:
        sys.exit("缺少标记价格或持仓模式，无法下单验证")
    if st["_pos_amt"] != 0 or st["_open_count"] > 0:
        sys.exit(f"{symbol} 上已有持仓或挂单，为避免干扰你现有的测试，本脚本拒绝下单。请换一个标的（--symbol）或先清理。")

    qty = max(spec["min_qty"], q_up(spec["min_notional"] * Decimal("1.2") / mark, spec["step"]))
    stop_px = q_down(mark * Decimal("0.80"), spec["tick"])  # 远低于现价，不会被触发
    tag = datetime.now(timezone.utc).strftime("%H%M%S")
    pos_side = "LONG" if dual else None
    reduce_flag = None if dual else "true"
    log(f"\n== 2–7. 下单验证（数量 {qty} {symbol}，约 {dec_str((qty * mark).quantize(Decimal('0.01')))} USDT）==")

    algo_ids: List[str] = []
    stray_order_ids: List[str] = []
    opened = False
    try:
        # 2. 不传 newOrderRespType 的市价单
        s, open_resp = c.post("/fapi/v1/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": dec_str(qty),
            "positionSide": pos_side or "BOTH", "newClientOrderId": f"hdprobe_{tag}_open",
        })
        record("2_market_open_default_resp", http=s, response=open_resp)
        log(f"  [2] 开仓回包 HTTP {s}: status={open_resp.get('status') if isinstance(open_resp, dict) else open_resp}"
            f", executedQty={open_resp.get('executedQty') if isinstance(open_resp, dict) else '-'}"
            f", avgPrice={open_resp.get('avgPrice') if isinstance(open_resp, dict) else '-'}")
        if s != 200 or not isinstance(open_resp, dict) or not open_resp.get("orderId"):
            verdict("E1", None, f"开仓失败，无法验证：{open_resp}")
            return
        opened = True
        oid = open_resp["orderId"]
        resp_filled = Decimal(str(open_resp.get("executedQty") or "0"))
        if open_resp.get("status") == "NEW" and resp_filled == 0:
            verdict("E1", True, "不传 newOrderRespType 时，市价单回包为 status=NEW、executedQty=0；项目会把已成交的单记成未成交")
        else:
            verdict("E1", False, f"回包已带成交结果（status={open_resp.get('status')}, executedQty={resp_filled}）")

        # 3. 查单与成交明细
        time.sleep(1.5)
        s, q = c.get("/fapi/v1/order", {"symbol": symbol, "orderId": oid}, signed=True)
        record("3_query_order", http=s, response=q)
        log(f"  [3] 查单: status={q.get('status') if isinstance(q, dict) else q}, executedQty={q.get('executedQty') if isinstance(q, dict) else '-'}")
        s, trades = c.get("/fapi/v1/userTrades", {"symbol": symbol, "orderId": oid}, signed=True)
        record("3_user_trades", http=s, response=trades)
        if isinstance(trades, list) and trades:
            fee = sum(Decimal(str(t.get("commission") or "0")) for t in trades)
            assets = sorted({t.get("commissionAsset") for t in trades})
            log(f"      成交明细 {len(trades)} 笔，手续费 {fee} {','.join(a for a in assets if a)}，成交编号 {[t.get('id') for t in trades]}")
            verdict("E8", True, f"交易所成交明细里有手续费 {fee} {','.join(a for a in assets if a)}，项目合约适配器记为空、从不拉取成交明细")
        else:
            verdict("E8", None, f"没有读到成交明细：{trades}")

        # 4. 旧接口下 STOP_MARKET
        s, old = c.post("/fapi/v1/order", {
            "symbol": symbol, "side": "SELL", "type": "STOP_MARKET", "quantity": dec_str(qty),
            "stopPrice": dec_str(stop_px), "workingType": "MARK_PRICE",
            "positionSide": pos_side or "BOTH", "reduceOnly": reduce_flag,
            "newClientOrderId": f"hdprobe_{tag}_oldsl",
        })
        record("4_legacy_stop_market", http=s, response=old)
        log(f"  [4] 旧接口止损单 HTTP {s}: {old}")
        if isinstance(old, dict) and old.get("code") == -4120:
            verdict("E4", True, "旧接口 /fapi/v1/order 下 STOP_MARKET 返回 -4120，项目的止损止盈挂不上")
        elif s == 200 and isinstance(old, dict) and old.get("orderId"):
            stray_order_ids.append(str(old["orderId"]))
            verdict("E4", False, "测试网旧接口仍接受 STOP_MARKET（主网是否一致需另行确认）")
        else:
            verdict("E4", None, f"旧接口返回了其他错误：{old}")

        # 5. Algo 接口下止损单
        s, algo = c.post("/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL", "type": "STOP_MARKET",
            "quantity": dec_str(qty), "triggerPrice": dec_str(stop_px), "workingType": "MARK_PRICE",
            "positionSide": pos_side or "BOTH", "reduceOnly": reduce_flag,
            "clientAlgoId": f"hdprobe_{tag}_algosl",
        })
        record("5_algo_stop_market", http=s, response=algo)
        log(f"  [5] Algo 接口止损单 HTTP {s}: {algo}")
        algo_id = str(algo.get("algoId")) if isinstance(algo, dict) and algo.get("algoId") else None
        if algo_id:
            algo_ids.append(algo_id)
            verdict("ALGO_OK", True, f"Algo 接口可以挂止损单（algoId={algo_id}），可作为 E4 的修复方式")
        else:
            verdict("ALGO_OK", None, f"Algo 接口下单失败，需要对照官方文档调整参数：{algo}")

        # 6. 普通挂单接口能否看到 / 撤掉 Algo 单
        if algo_id:
            _, oo = c.get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
            in_open = any(str(o.get("orderId")) == algo_id or o.get("clientOrderId") == f"hdprobe_{tag}_algosl"
                          for o in (oo or []) if isinstance(o, dict))
            s_cancel, cancel_all = c.delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            time.sleep(1.0)
            _, ao = c.get("/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
            algo_list = ao if isinstance(ao, list) else (ao.get("orders") if isinstance(ao, dict) else None) or []
            still = any(str(o.get("algoId")) == algo_id for o in algo_list if isinstance(o, dict))
            record("6_visibility", in_open_orders=in_open, cancel_all_http=s_cancel, cancel_all_response=cancel_all,
                   algo_still_open_after_cancel_all=still, open_algo_orders=ao)
            log(f"  [6] Algo 单出现在 openOrders 中: {in_open} | 调用 allOpenOrders 后仍存在: {still}")
            if not in_open and still:
                verdict("E4_CANCEL", True, "Algo 条件单不出现在普通挂单列表，撤销全部普通挂单也撤不掉；项目的查单和撤单需要改走 Algo 接口")
            elif not still:
                verdict("E4_CANCEL", False, "撤销全部普通挂单时连带撤掉了 Algo 条件单")
            else:
                verdict("E4_CANCEL", None, "Algo 单出现在普通挂单列表中，需人工查看结果文件")
    finally:
        # 清理：撤销 Algo 单与误挂的普通条件单
        for aid in algo_ids:
            s, r = c.delete("/fapi/v1/algoOrder", {"symbol": symbol, "algoId": aid})
            record(f"cleanup_algo_{aid}", http=s, response=r)
        for soid in stray_order_ids:
            c.delete("/fapi/v1/order", {"symbol": symbol, "orderId": soid})

        # 7. 用 newOrderRespType=RESULT 平仓
        if opened:
            _, pos = c.get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
            amt = sum(Decimal(str(p.get("positionAmt") or "0")) for p in (pos or []) if isinstance(p, dict)
                      and (not dual or p.get("positionSide") == "LONG"))
            if amt > 0:
                s, close = c.post("/fapi/v1/order", {
                    "symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": dec_str(amt),
                    "positionSide": pos_side or "BOTH", "reduceOnly": reduce_flag,
                    "newOrderRespType": "RESULT", "newClientOrderId": f"hdprobe_{tag}_close",
                })
                record("7_market_close_result_resp", http=s, response=close)
                log(f"  [7] 平仓回包（RESULT）HTTP {s}: status={close.get('status') if isinstance(close, dict) else close}"
                    f", executedQty={close.get('executedQty') if isinstance(close, dict) else '-'}"
                    f", avgPrice={close.get('avgPrice') if isinstance(close, dict) else '-'}")
                filled = Decimal(str(close.get("executedQty") or "0")) if isinstance(close, dict) else Decimal("0")
                has_px = isinstance(close, dict) and (
                    Decimal(str(close.get("avgPrice") or "0")) > 0 or Decimal(str(close.get("cumQuote") or "0")) > 0
                )
                if filled > 0 and has_px:
                    verdict("RESULT_OK", True, "传 newOrderRespType=RESULT 后，市价单回包直接带成交数量和均价，可作为 E1 的修复方式")
                elif filled > 0:
                    verdict("RESULT_OK", False,
                            "RESULT 回包带成交数量和 FILLED 状态，但没有 avgPrice / cumQuote；"
                            "项目会把订单记为已成交却不写成交记录，仍需下单后查单或查成交明细补齐价格和手续费")
                else:
                    verdict("RESULT_OK", None, f"RESULT 回包未带成交结果：{close}")

        # 最终检查
        _, pos = c.get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        left = sum(abs(Decimal(str(p.get("positionAmt") or "0"))) for p in (pos or []) if isinstance(p, dict))
        _, ao = c.get("/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        algo_left = ao if isinstance(ao, list) else (ao.get("orders") if isinstance(ao, dict) else None) or []
        record("final", position_left=str(left), algo_orders_left=algo_left)
        if left == 0 and not algo_left:
            log("\n  清理完成：持仓已平，条件单已撤。")
        else:
            log(f"\n  ⚠️ 清理未完成：剩余持仓 {left}，剩余 Algo 单 {len(algo_left)}，请到测试网页面手动处理 {symbol}。")


# ------------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="币安 U 本位合约测试网验证（审计报告 10.5）")
    ap.add_argument("--symbol", default="ETHUSDT", help="验证用的标的，默认 ETHUSDT")
    ap.add_argument("--trade", action="store_true", help="执行下单验证（默认只做只读检查）")
    ap.add_argument("--yes", action="store_true", help="跳过 YES 确认（不推荐）")
    args = ap.parse_args(argv)
    symbol = args.symbol.upper()

    key = os.environ.get("HEIMDALL_PROBE_API_KEY", "").strip()
    secret = os.environ.get("HEIMDALL_PROBE_API_SECRET", "").strip()
    if not key or not secret:
        sys.exit("请先设置环境变量 HEIMDALL_PROBE_API_KEY 和 HEIMDALL_PROBE_API_SECRET（币安合约测试网密钥）")
    RESULT["api_key"] = mask(key)
    RESULT["symbol"] = symbol
    log(f"API Key: {mask(key)} | 标的: {symbol} | 模式: {'下单验证' if args.trade else '只读检查'}")

    base = probe_hosts(key, secret)
    out_path = None
    try:
        if not base:
            log("\n没有可用的测试网地址，后续检查跳过。")
            return
        RESULT["base_url"] = base
        c = Client(base, key, secret)
        c.sync_time()
        st = readonly_checks(c, symbol)
        if args.trade:
            if not args.yes:
                ans = input(f"\n即将在 {base} 上用 {symbol} 下一笔小额市价单并自动平仓，输入 YES 继续：").strip()
                if ans != "YES":
                    log("已取消下单验证。")
                    return
            trade_checks(c, symbol, st)
        else:
            log("\n只读检查完成。加 --trade 执行下单验证（E1 / E4 / E8）。")
    finally:
        RESULT["finished_at"] = datetime.now(timezone.utc).isoformat()
        out_dir = ROOT / "data" / "testnet_probe"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out_path.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log(f"\n== 结论汇总 ==")
        for code, v in RESULT["verdicts"].items():
            tag = {True: "确认", False: "不成立", None: "无法判断"}[v["confirmed"]]
            log(f"  {code:<10} {tag:<5} {v['detail']}")
        log(f"\n完整结果已保存: {out_path}")


if __name__ == "__main__":
    main()
