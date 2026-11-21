# 安全与量化严谨性代码审查

**审查日期：** 2026-11-21  
**审查范围：** Binance 执行适配器、历史资金费率处理、回测止损撮合  
**审查原则：** 安全优先，量化假设必须透明且可验证

## 结论

发现 3 个需要优先处理的问题。当前实现不能作为真实资金实盘执行和回测评估的可靠依据。

## 问题清单

### [P0] Binance `MAINNET` 模式没有提交实盘订单，却会在本地记为成交

**位置：** `heimdall/execution/binance.py:158-180`

远程下单逻辑只处理 Demo 和 Testnet。适配器虽然会为 `MAINNET` 配置主网地址，但没有对应的主网下单分支。执行流程随后仍会进入本地撮合并生成成交回报。

**影响：** 机器人可能报告订单已成交，但交易所并没有收到对应订单；本地持仓、净值和订单保护状态也可能与真实账户不一致。这会造成严重的实盘操作误判。

**建议：** 补齐主网下单、订单状态查询、成交量确认和错误处理链路，并确保主网请求失败时不能静默回退为本地模拟成交。在这条链路通过针对性验证之前，不要将 Binance `MAINNET` 用于实盘。

### [P1] 缺失历史资金费率时，回测擅自假设固定正费率

**位置：** `heimdall/backtest/feed.py:63-88, 90-105`

当历史费率序列没有覆盖目标时间时，`funding_at()` 会返回固定 `0.0001` 费率。若 K 线跨过结算时点，`funding_event_in()` 也会用这个默认值结算资金费。

**影响：** 回测会把假设值当作历史观测，可能凭空增加或扣除收益，使资金费套利策略判断、策略比较和绩效指标失真。

**建议：** 缺少历史费率时应标记数据不完整，并明确选择跳过资金费结算、拒绝该段回测，或采用有来源且显式声明的估算。报告中应披露费率数据覆盖情况，避免将估算结果表述为实测历史绩效。

### [P1] 回测止损忽略跳空，可能系统性低估亏损

**位置：** `heimdall/backtest/broker.py:231-277`

止损触发后，撮合价格始终取止损价。若下一根 K 线开盘价已经越过止损价，代码仍以止损价成交，没有模拟止损市价单在跳空时可能发生的更差成交。

**影响：** 回测可能低估止损损失、最大回撤和尾部风险，进而高估策略的风险调整后收益。

**建议：** 明确止损订单的成交模型；发生跳空时，按开盘价或其他保守、可解释的价格模型成交，并在报告中记录这一撮合假设。同一根 K 线同时触及止损和止盈时，也应保留并说明当前“止损优先”的保守规则。

## 审查说明

- 工作区审查时没有未提交改动。
- 本次没有运行测试；以上结论来自静态代码路径审查。
- 修复后应针对主网执行、缺失费率数据和止损跳空分别增加覆盖对应边界条件的验证。

---

## 整改完成记录 (2026-11-21)

针对审查发现的 3 个问题，已全部完成代码整改与边界测试覆盖：

### 1. [P0] Binance `MAINNET` 实盘链路安全闭环
- **整改文件：** `heimdall/execution/binance.py`
- **实现方案：**
  - 在 `MAINNET` 实盘模式下强制校验 `api_key` 与 `secret_key`，缺失时直接抛出 `RuntimeError`，绝不进入未授权状态执行或静默模拟；
  - 接入 `POST /fapi/v1/order` 实际报单请求，正确解析真实 `orderId`、`executedQty`、`avgPrice`；
  - 远程接口请求失败时坚决上抛异常，杜绝静默回退至本地沙盒模拟盘；
  - 补齐主网持仓查询 (`GET /fapi/v2/positionRisk`)、账户余额 (`GET /fapi/v2/account`)、条件挂单 (`STOP_MARKET`)、查单与撤单链路。

### 2. [P1] 历史资金费率缺失处理严谨化
- **整改文件：** `heimdall/backtest/feed.py`, `heimdall/backtest/metrics.py`, `heimdall/backtest/engine.py`
- **实现方案：**
  - 彻底移除对缺失费率时擅自假设 `0.0001` 的逻辑；无历史序列时 `funding_at` 返回 `funding_rate: 0.0`，并显式标记 `has_data: False` 与 `is_estimated: False`；
  - `funding_event_in` 在缺失真实费率时默认返回 `None`（跳过资金费结算），除非用户显式配置了 `default_funding_rate`；
  - 新增 `funding_coverage_stats()` 方法核算费率数据覆盖率，并在 `BacktestReport` 的 `funding_coverage` 字段中透明披露结算模式（如 `REAL_HISTORY`, `SKIPPED_MISSING_DATA`, `ESTIMATED`）。

### 3. [P1] 回测止损跳空保守撮合模型
- **整改文件：** `heimdall/backtest/broker.py`, `heimdall/backtest/metrics.py`
- **实现方案：**
  - 多头持仓止损：若次根 K 线开盘价低开越过止损价（跳空低开），撮合价格取更差的开盘价 `bar_open`（并附加滑点），标记 `INTRABAR_SL_GAP`；未跳空时按止损价撮合；
  - 空头持仓止损：若次根 K 线开盘价高开越过止损价（跳空高开），撮合价格取更差的开盘价 `bar_open`（并附加滑点），标记 `INTRABAR_SL_GAP`；
  - 保留并声明同根 K 线同时触及 SL 与 TP 时“止损优先”的保守原则；
  - `BacktestReport` 增加 `execution_assumptions` 字段，公开披露撮合假设。

### 4. 自动化测试验证
- **测试套件：** `tests/test_security_quant_remediation.py`（9 项针对性用例全部通过）
- **回归测试：** `tests/test_binance_demo_adapter.py` 与 `tests/test_strategy_router_and_backtest.py` 全部通过（共 21 项测试通过）。
