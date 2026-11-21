/**
 * ================================================================================
 * Heimdall 交易平台 - 前端仿真模拟数据与全量 Agent 角色知识库
 * ================================================================================
 * 
 * 【小白速懂】：
 * 本文件汇集了 9 大专业智能体角色的元数据定义、决策理由、置信度以及实盘 K 线时序数据。
 * 基准价格校准为当前真实以太坊市场价位区间 (~$1,744.00)。
 */

export const INITIAL_AGENTS = [
  {
    id: "analyst",
    name: "AnalystAgent",
    title: "技术面宏观分析师",
    avatar: "📊",
    color: "#3E6B89",
    tag: "TREND_UP · 多头突破",
    badge: "置信度 80%",
    seatPosition: "top-center",
    stance: "LONG",
    confidence: 0.80,
    roleDescription: "【小白速懂】：负责阅读纯 K 线价格走势，计算均线（EMA）、强弱指标（RSI）、真实波幅（ATR）等量化数学指标，判断当前大盘是单边暴涨、单边暴跌还是横盘拉锯。",
    keyThesis: "15m 周期呈现标准多头排列，价格放量突破 1,740 颈线阻力位，EMA20 上穿 EMA50 形成经典金叉，短期多头动能充盈。",
    evidence: [
      "EMA20 ($1,735.5) > EMA50 ($1,722.0)，斜率 +14.2° 持续上扬",
      "RSI14 当前值为 58.4，处于健康强势区间，未触及 70 超买红线",
      "ATR 波动率为 $18.5，处于近 24 小时常态范围，无异常插针剧震",
      "K 线连续 3 根收出实体阳线，量能放大 1.45 倍"
    ],
    invalidation: "若 15m 收盘价跌破 EMA20 支撑位 ($1,730.0)，则技术突破立论失效，建议止损。",
    thinkingMonologue: "正在扫描 Bifrost 数据底座 60 根闭合 K 线... 剔除前视偏差后，确认上一根 K 线收盘价 1,744.50 高于过去 48 根 K 线的均值。动量指标共振，批准生成看多技术简报。"
  },
  {
    id: "sentiment",
    name: "SentimentWhaleAnalyst",
    title: "情绪与链上巨鲸分析师",
    avatar: "🐋",
    color: "#4D835C",
    tag: "吸筹锁仓 · 情绪拐头",
    badge: "得分 +0.45",
    seatPosition: "top-left",
    stance: "LONG",
    confidence: 0.78,
    roleDescription: "【小白速懂】：专门抓取交易所大户充提币动向（筹码锁仓还是砸盘抛售）、全网恐慌贪婪指数、未平仓合约量 (OI) 与稳定币印钞弹药，防止掉入诱多踩踏陷阱。",
    keyThesis: "全网恐慌贪婪指数从 18 极低点拐头修复至 24，大户连续 24 小时从币安/OKX 提现 1,250 万美元 ETH 进冷钱包，抛压急剧衰竭，筹码良性锁定。",
    evidence: [
      "Alternative.me 恐慌指数 7 天趋势为 [28 -> 22 -> 18 -> 19 -> 24]，低位探底回升",
      "链上巨鲸 24h 净流入为 -$12,500,000 USD（负数代表大额提币锁仓）",
      "全网主流稳定币 (USDT/USDC) 24h 净增发铸造 8,500 万 USD，场外弹药充盈",
      "Derivatives OI 稳定在健康安全区间，未出现空头集中踩踏"
    ],
    invalidation: "若监测到巨鲸单笔大额充值进币安超过 20,000 ETH，立论转向中性防守。",
    thinkingMonologue: "链上图谱分析完成：巨鲸冷钱包持续流出，衍生品资金费率平稳在年化 4.5% 水平，确认无拥挤多头多杀多隐患。"
  },
  {
    id: "bull",
    name: "BullResearcher",
    title: "多头先锋研究员",
    avatar: "🐂",
    color: "#4D835C",
    tag: "目标 $1,820 · 胜率 68%",
    badge: "做多置信 85%",
    seatPosition: "mid-left",
    stance: "LONG",
    confidence: 0.85,
    roleDescription: "【小白速懂】：多空辩论里的“正方辩友”。拼命寻找上涨的所有蛛丝马迹，给出坚定的做多买入理由、预估止盈目标价，并测算出合理的风险报酬比（盈亏比）。",
    keyThesis: "现货买盘真实放量，1,750 处空头止损极为密集。一旦突破该关键点位，将引发多米诺骨牌式的空头踩踏轧空（Short Squeeze），第一阻力止盈位看至 $1,820。",
    evidence: [
      "深度订单簿 (OrderBook) 显示 1,735 附近累积有 4,800 枚 ETH 密集支撑买盘",
      "买卖量比（Taker Buy/Sell Ratio）连续 3 小时维持在 1.48 高位",
      "潜在盈利空间 $75.5 / 止损空间 $34.5 = 盈亏比 2.19:1，符合 > 2.0 准入要求"
    ],
    invalidation: "若 15m 价格放量跌破 1,710 美元颈线关键支撑，判定突破失败，无条件止损。",
    thinkingMonologue: "多头立论质询完成：买盘力量扎实，现货与永续基差正向收敛，极大概率走出单边上攻行情，建议挂单 1,740 入场。"
  },
  {
    id: "bear",
    name: "BearResearcher",
    title: "空头质询研究员",
    avatar: "🐻",
    color: "#C24D4D",
    tag: "防守 $1,710 · 警惕冲高回落",
    badge: "空头置信 45%",
    seatPosition: "mid-right",
    stance: "SHORT",
    confidence: 0.45,
    roleDescription: "【小白速懂】：多空辩论里的“反方辩友”与找茬专家。无论形势多好，它总能挑出潜在风险，比如上方抛压、宏观美联储议息会议、主力假突破诱多，逼迫团队做好防守。",
    keyThesis: "上方 1,780~1,800 密集套牢筹码依然沉重；若冲高成交量未能持续放大，极易走出上吊线或长上影假突破，建议将硬止损紧咬在 1,710 防止高位套牢。",
    evidence: [
      "过去两周在 1,780 关口曾有两次未能突破形成的‘双顶假突破’历史",
      "1 小时级别 RSI 处于 65 偏高水位，若无增量现货跟进，存在钝化背离可能",
      "今晚 20:30 美联储官员将发表通胀讲话，宏观波动率可能突增"
    ],
    invalidation: "若 15m 价格站稳 1,760 且成交量超越上一周期 2 倍，则空头防守假设推翻，空头弃权投降。",
    thinkingMonologue: "空头风险排查：上方压力确实存在，但多头现货买盘支撑有力。裁定：可以做多，但必须收紧止损至 1,710，杜绝抗单！"
  },
  {
    id: "moderator",
    name: "DebateModerator",
    title: "多空博弈首席裁判",
    avatar: "⚖️",
    color: "#DE6438",
    tag: "裁决: 批准发起做多",
    badge: "综合置信 82%",
    seatPosition: "bottom-center",
    stance: "LONG",
    confidence: 0.82,
    roleDescription: "【小白速懂】：法官与终审大裁判。它不偏袒任何一方，主持多头研究员与空头研究员的 3 轮激辩，综合双方证据得出最终结论（买、卖还是空仓观望）。",
    keyThesis: "经过 2 轮对抗质询，多方立论（现货买盘 + 巨鲸锁仓）得到量化确认；空方提出的阻力风险已通过‘将止损由 1,680 上移至 1,710’得到充分对冲。裁定：批准做多！",
    evidence: [
      "多空辩论焦点收敛：一致认可当前盈亏比达 2.19:1",
      "空方提出的回踩风险已融入止损策略，防守位置安全",
      "综合双方论据后多头加权胜率评分为 82 分（阈值 > 70 分）"
    ],
    invalidation: "博弈结论仅在当前 15m 周期内有效；若下一周期市场发生突发巨鲸砸盘，将自动重新开庭裁判。",
    thinkingMonologue: "主持辩论终审：双方立论清晰，争议点已收敛。已生成结构化辩论纪要，交由策略师生成具体执行委托单。"
  },
  {
    id: "strategy",
    name: "StrategyAgent",
    title: "定量执行策略师",
    avatar: "🎯",
    color: "#D48B38",
    tag: "挂单 1,740 · 止盈 1,820",
    badge: "半凯利仓位 25%",
    seatPosition: "bottom-left",
    stance: "LONG",
    confidence: 0.90,
    roleDescription: "【小白速懂】：精算师。它拿到裁判的做多批文后，不盲目市价追高，而是计算最佳挂单价、利用凯利公式（Kelly Criterion）计算该买多少钱，绝不多买哪怕一分钱。",
    keyThesis: "将主观意志转化为确定性数学公式：入场限价 $1,740.00，硬止损 $1,710.00，止盈 $1,820.00，使用 1/2 凯利公式测算头寸价值为 $2,616.00 USD (1.5 ETH)。",
    evidence: [
      "入场限价单设在突破颈线回踩位 $1,740.00，避免激进市价单产生过多滑点",
      "硬止损价 $1,710.00，单笔最大预期风险暴露为 $45.00 USD (< 1% 账户总资产)",
      "头寸规模根据当前胜率 68% 与盈亏比 2.19 计算，取安全系数 0.5"
    ],
    invalidation: "若挂单 30 分钟未被交易所成交撮合，订单自动撤销。",
    thinkingMonologue: "数学模型推导完成：已生成带有全局唯一 Idempotency-Key 的交易提案对象，递交七大铁律确定性硬风控审核。"
  },
  {
    id: "risk_officer",
    name: "RiskOfficerAgent",
    title: "合规风控官",
    avatar: "🛡️",
    color: "#8C7A6B",
    tag: "盈亏比 2.19:1 · 审查通过",
    badge: "软风控已核准",
    seatPosition: "top-right",
    stance: "PASS",
    confidence: 0.95,
    roleDescription: "【小白速懂】：大管家。审查策略师拿来的订单提案，核对盈亏比是否划算、止损是否设得太宽导致暴亏、以及是不是违背了账户的投资组合纪律。",
    keyThesis: "审核委托提案：目标价格设定合规，盈亏比 2.19:1 优于系统最低底线 1.8:1，单笔潜在损失仅占账户权益的 0.85%，在安全预算之内。签字放行！",
    evidence: [
      "单笔风险敞口 (Risk Exposure): $45.00 <= $100.00 (通过)",
      "潜在盈亏比 (R:R Ratio): 2.19:1 >= 1.80:1 (通过)",
      "杠杆倍数 (Leverage): 1.5x <= 3.0x (通过)"
    ],
    invalidation: "一旦账户当日累计回撤触及 3.5%，风控官将收紧准入，不再批准任何新开仓提案。",
    thinkingMonologue: "软逻辑合规性审查完毕：无越权高危行为，提案结构合法，移交底层确定性硬风控守护神进行物理规则卡口拦截。"
  },
  {
    id: "hard_guard",
    name: "HardRiskGuard",
    title: "七大铁律确定性硬风控",
    avatar: "🔒",
    color: "#4D835C",
    tag: "7/7 铁律全票放行",
    badge: "确定性 100%",
    seatPosition: "bottom-right",
    stance: "PASS",
    confidence: 1.0,
    roleDescription: "【小白速懂】：绝对零妥协的物理断路器！用纯 Python 代码写死，哪怕大模型疯了、幻觉了、想做空地球，这道防火墙只要检测到 1 条不合格，一微秒内直接拔插头拦截！",
    keyThesis: "纯确定性规则守护！逐项校验系统七大不可违背铁律：今日回撤 1.2% (<5%)、Bifrost 基差 0.03% (<0.5%)、杠杆 1.5x (<3.0x) 等，全票核准下发！",
    evidence: [
      "✓ 铁律1: 日内最大回撤 1.2% < 5.0% 熔断阈值",
      "✓ 铁律2: Bifrost 跨所永续价差 0.03% < 0.50% 异常阈值",
      "✓ 铁律3: 策略开仓杠杆 1.5x < 3.0x 硬上限",
      "✓ 铁律4: 单笔头寸 $2,616.00 < $5,000.00 单笔上限",
      "✓ 铁律5: 预估执行滑点 0.10% < 0.30% 允许偏差",
      "✓ 铁律6: 宏观事件避险日历检测（当前处于静默期外）",
      "✓ 铁律7: 单根 K 线振幅 0.95% < 3.00% 极端插针防范"
    ],
    invalidation: "永不失效。只要任意 1 项指标越界，毫秒级直接抛出 RiskViolationException 并终止执行。",
    thinkingMonologue: "确定性硬风控执行巡检：7 项指标无一越界。已对本次订单签发防重放 SHA256 签名，准许撮合执行！"
  },
  {
    id: "alert_notifier",
    name: "AlertNotifier",
    title: "全天候事件与熔断通知官",
    avatar: "📢",
    color: "#DE6438",
    tag: "多端通知 · 实时在线",
    badge: "Telegram/飞书",
    seatPosition: "sub-control",
    stance: "MONITOR",
    confidence: 1.0,
    roleDescription: "【小白速懂】：全天候情报通讯兵。一旦系统触发熔断锁仓、订单撮合成交、或者跨所行情插针异动，0.5 秒内推送到你的手机微信、飞书或 Telegram 上！",
    keyThesis: "实时监听系统各总线事件，当前就绪连接：飞书 Webhook (活跃)、Telegram Bot (已认证)、Discord (待命)。",
    evidence: [
      "推送通道 1: 飞书机器人群组通知 (已接入)",
      "推送通道 2: Telegram @HeimdallTradingBot (已配对)",
      "通道延迟: 平均 HTTP Webhook 耗时 120ms",
      "防骚扰机制: 相同警告 5 分钟内自动合并去重"
    ],
    invalidation: "断网时自动平滑回退至控制台本地日志记录。",
    thinkingMonologue: "订单成交事件已捕获：ETH 开多 1.5 枚 @ 1,740.00 已就绪，准备组装富文本卡片广播至移动端群聊。"
  }
];

/**
 * 初始 30 根 15m 蜡烛线行情数据（校准至 ~$1,744 真实盘口，用于 K 线图离线渲染）
 */
export const INITIAL_KLINES = [
  { time: 1710000000, open: 1718.0, high: 1726.0, low: 1715.0, close: 1722.0, volume: 1420 },
  { time: 1710000900, open: 1722.0, high: 1730.0, low: 1719.0, close: 1728.0, volume: 1680 },
  { time: 1710001800, open: 1728.0, high: 1734.0, low: 1725.0, close: 1727.0, volume: 1120 },
  { time: 1710002700, open: 1727.0, high: 1731.0, low: 1720.0, close: 1724.0, volume: 980 },
  { time: 1710003600, open: 1724.0, high: 1728.0, low: 1718.0, close: 1720.0, volume: 1350 },
  { time: 1710004500, open: 1720.0, high: 1725.0, low: 1716.0, close: 1723.0, volume: 1040 },
  { time: 1710005400, open: 1723.0, high: 1735.0, low: 1721.0, close: 1732.0, volume: 1890 },
  { time: 1710006300, open: 1732.0, high: 1740.0, low: 1729.0, close: 1738.0, volume: 2150 },
  { time: 1710007200, open: 1738.0, high: 1745.0, low: 1734.0, close: 1741.0, volume: 1980 },
  { time: 1710008100, open: 1741.0, high: 1746.0, low: 1737.0, close: 1742.0, volume: 1460 },
  { time: 1710009000, open: 1742.0, high: 1748.0, low: 1739.0, close: 1743.0, volume: 1620 },
  { time: 1710009900, open: 1743.0, high: 1750.0, low: 1740.0, close: 1746.0, volume: 2400 },
  { time: 1710010800, open: 1746.0, high: 1752.0, low: 1742.0, close: 1744.5, volume: 1850 },
];
