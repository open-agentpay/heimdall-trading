# 📋 Heimdall 量化交易系统 - 核心待办事项清单 (TODO)

> **创建时间**：2026-11-19  
> **当前状态**：进行中 (In Progress)  
> **维护原则**：按优先级（P0 核心基座 → P1 业务功能 → P2 体验优化）分类跟进，完成一项勾选一项。

---

## 一、 Web3 钱包登录与密钥金库待办 (Authentication & Key Vault)

- [x] **1.1 小狐狸 (MetaMask) / OKX 真实签名弹窗对接**
  - [x] 前端唤起小狐狸插件并读取公钥地址 (`eth_requestAccounts`)
  - [x] 前端调用后端 `GET /api/v1/auth/nonce?address=0x...` 获取防重放挑战明文
  - [x] 前端使用 `window.ethereum.request({ method: 'personal_sign', params: [message, address] })` 真实弹出小狐狸签名确认窗口
  - [x] 监听钱包账户切换 (`accountsChanged`) 与链网络切换 (`chainChanged`) 事件，自动同步登录态

- [x] **1.2 钱包登录前后端联调与 Session 鉴权**
  - [x] 后端 `eth_account` 密码学 EIP-191 验签逻辑与 Nonce 轮换机制
  - [x] 前端将签名结果 POST 至 `/api/v1/auth/verify` 进行真实验签
  - [x] 前端保存返回的 `session_token` 到本地 `localStorage`，并在后续请求头中携带 `Authorization: Bearer <token>`
  - [x] 退出登录时销毁本地与服务端会话并清理 Authorization 头

- [x] **1.3 密钥管理金库前后端 API 联调 (Key Vault)**
  - [x] 后端 AES-256-GCM 硬件认证加密存储与数据库建表 (`user_api_keys`)
  - [x] 前端密钥脱敏展示、明文切换查看与新增表单 UI
  - [x] 前端列表对接后端 `GET /api/v1/vault/keys`（支持从后端数据库动态读取并脱敏展示）
  - [x] 前端“新增凭证”提交至 `POST /api/v1/vault/keys`，自动绑定后端实际返回的 `key_id`
  - [x] 前端封装并打通 `DELETE /api/v1/vault/keys/{id}` 彻底销毁凭证接口，并联动本地缓存阻止预置凭证重复复现，解决“删除不生效”问题
  - [x] 后端增强 `POST /api/v1/vault/keys/{id}/test` 探针自适应能力，支持预置与真实入库凭证连通性测试，彻底解决“测试404”报错问题

---

## 二、 数据链路与前后端联调层 (P0 核心基座)

- [x] **2.0 多源行情底座重构与容灾降级 (Market Data Hub & Failover)**
  - [x] 将原假 Bifrost 服务明确标记为 `TODO` 规划存根，停止直连无效本地 8080 端点
  - [x] 接入真实币安 (Binance) 官方数据源 (`BinanceMarketDataProvider`)，支持 USD-M 永续合约与现货免 Key 毫秒级 K 线、资金费率与最新标记价格
  - [x] 实现严格零前视偏差（Zero-Lookahead Bias）保护，自动剔除未闭合的在途 K 线，支持时光机 (`as_of_ts`) 截断
  - [x] 接入 Hyperliquid DEX 官方公共 API (`HyperliquidMarketDataProvider`)，支持全网 Mid 报价、资金费率与蜡烛快照
  - [x] 实现多源互补与全链路自动降级中枢 (`CompositeMarketDataProvider` / `MarketDataHub`)：Binance 主源 -> Hyperliquid 辅源 -> 离线沙盒自适应兜底
  - [x] 实现跨交易所实时盘口价差守卫 (`check_cross_exchange_guard`)：基于 Binance vs Hyperliquid 真实价差划分 4 级状态 (HEALTHY / WARNING / DEGRADED / ISOLATED)，并支持单源存活平滑降级
  - [x] 后端补充 `/api/v1/market/klines`、`/api/v1/market/funding`、`/api/v1/market/guard` 及 `/v1/` 兼容路由
  - [x] 前端 `KlineChart` 接入后端多源聚合 K 线接口

- [x] **2.1 REST API 通信层集成**
  - [x] 封装前端请求库 `web/src/api/client.js` 与 `web/src/api/endpoints.js`
  - [x] 真实对接后端 `/api/v1/bots` 读取机器人列表与健康探针
  - [x] 真实对接 `/api/v1/bots/{id}/step` 与 `latest-state` 实现单步强制推演 (`force=True`) 与对弈台数据毫秒级同步
  - [x] 修复对弈台（Decision Arena）数据不刷新问题：新增 4s 静默自动轮询机制与顶栏「推演刷新」手动按钮，全量去除牌桌静态硬编码文本，真实动态绑定多 Agent 辩论成果
  - [x] 真实对接 `/api/v1/emergency/kill` 实现全屏红线一键急停与全撤单

- [x] **2.2 WebSocket 实时推流中枢**
  - [x] 后端 FastAPI 新增 `/ws/stream` 长链接路由与广播发布订阅（Pub/Sub）机制 (`ConnectionManager`)
  - [x] 前端实现具备指数退避重连与心跳保活的 `useWebSocket` Hook
  - [x] 实时推送数据流：
    - 即时 K 线高频跳动报价
    - 多 Agent 多空辩论实时推演记录与 StepTraces 同步
    - 7 大极端行情硬风控拦截事件告警与一键急停广播
    - 真实/沙盒订单撮合成交通知

- [x] **2.3 全局状态流转 (State Store)**
  - [x] 建立 `BotContext`（当前选中的机器人、标的、周期、推演状态、流水）
  - [x] 建立 `RiskContext`（熔断报警状态、黑天鹅避险倒计时、雷达动态遥测）

---

## 三、 核心业务子页面视图补齐 (P1 核心功能)

- [x] **3.1 「痕迹溯源 / Traces」审计页面**
  - [x] 决策历史时间轴列表（支持按标的、时间、多空方向筛选）
  - [x] 展开查看 Bull（多头）、Bear（空头）、Moderator（裁判）的完整提示词与思考独白（COT）
  - [x] 策略裁判裁决打分矩阵与风控官驳回/批准硬规则依据与微秒级 StepTrace 耗时

- [x] **3.2 「资产表现 & 绩效 / Leaderboard & NAV」页面**
  - [x] 交互式单位净值曲线图（支持 1D / 1W / 1M / ALL 时间切换，支持叠加基准）
  - [x] 量化核心绩效看板：夏普比率、索提诺比率、最大回撤、胜率、盈亏比
  - [x] 成交流水对账表格：开平仓时间、均价、滑点、手续费、实现盈亏

- [x] **3.3 「蜂群编排 / Swarm」集群管理页面**
  - [x] 运行中策略机器人卡片墙与启停控制 (`SwarmView.jsx`)
  - [x] 一键批量全部启动 / 批量全部挂起
  - [x] 新建/配置机器人抽屉与组合资金配比调整弹窗联动

- [x] **3.4 「情绪与多源情报 / Sentiment」页面**
  - [x] 恐慌贪婪指数历史展示与 7 日低位拐头动量
  - [x] 链上巨鲸充提流向监测与大额异动转账
  - [x] 衍生品持仓多空比、年化资金费率与突发快讯黑天鹅排查

---

## 四、 专业交易图表与极端避险雷达 (P1 专业体验)

- [x] **4.1 K 线图表专业化增强**
  - [x] 直连交易所公开 WebSocket 行情源（免 Key 获取分时蜡烛）
  - [x] 叠加技术指标图层：EMA(20, 50) 动态均线、成交量副图、RSI 动力学指标
  - [x] 盘面交易打点：多空开平仓标记点（↑ BUY / ↓ SELL）、持仓均价水平线、止盈止损警戒线

- [x] **4.2 7 大极端行情避险雷达看板**
  - [x] 后端新增 `/api/v1/market/radar` 动态遥测聚合接口
  - [x] 规则 1：宏观重大事件倒计时时钟（距 CPI/FOMC 会议倒计时，锁定窗口黄色预警条）
  - [x] 规则 2：分钟级暴涨暴跌插针监测仪（分钟振幅超过 2.5% 标红报警）
  - [x] 规则 3：超长上下影线天地针清洗排查指示器
  - [x] 规则 4：盘口流动性黑洞与买卖点差监视仪（Spread Bps 异常报警）
  - [x] 规则 5：异常巨额负资金费率与多头挤压预警
  - [x] 规则 6：全市场杠杆爆仓热度潮监控
  - [x] 规则 7：底层网关 RPC 延迟与心跳脱节指示灯
  - [x] 一键全屏紧急急停与全撤单控制中心真实对接后端 API

---

## 五、 交互体验与工程化完善 (P2 体验打磨)

- [x] **5.1 暗黑/暖沙主题切换 (Theme Switcher)**
  - 默认保持暖沙工匠质感，增加夜间盯盘专用的暗黑量化终端主题
- [x] **5.2 声音与系统通知 (Audio & Web Notifications)**
  - 发生极端行情拦截或订单成交时触发浏览器系统通知与短音效
- [x] **5.3 响应式小屏适配**
  - 针对平板与小屏幕优化侧边栏与终端栏的折叠抽屉模式
