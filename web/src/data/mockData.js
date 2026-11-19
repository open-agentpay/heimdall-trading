/**
 * ================================================================================
 * Heimdall 交易平台 - 前端仿真模拟数据与全量 Agent 角色知识库
 * ================================================================================
 * 
 * 【小白速懂】：
 * 本文件汇集了 9 大专业智能体角色的元数据定义、决策理由、置信度以及实盘 K 线时序数据。
 * 在后端 WebSocket 未连接或本地离线预览时，提供逼真流畅的动态交互体验。
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
    keyThesis: "15m 周期呈现标准多头排列，价格放量突破 3,120 颈线阻力位，EMA20 上穿 EMA50 形成经典金叉，短期多头动能充盈。",
    evidence: [
      "EMA20 ($3,108.5) > EMA50 ($3,092.0)，斜率 +14.2° 持续上扬",
      "RSI14 当前值为 58.4，处于健康强势区间，未触及 70 超买红线",
      "ATR 波动率为 $32.5，处于近 24 小时常态范围，无异常插针剧震",
      "K 线连续 3 根收出实体阳线，量能放大 1.45 倍"
    ],
    invalidation: "若 15m 收盘价跌破 EMA20 支撑位 ($3,108.0)，则技术突破立论失效，建议止损。",
    thinkingMonologue: "正在扫描 Bifrost 数据底座 60 根闭合 K 线... 剔除前视偏差后，确认上一根 K 线收盘价 3,124.50 高于过去 48 根 K 线的均值。动量指标共振，批准生成看多技术简报。"
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
      "多空大户人数比为 1.05，尚未出现 >1.8 的危险拥挤扎堆"
    ],
    invalidation: "若监测到巨鲸单笔向交易所充值超过 5,000 ETH，则筹码锁仓逻辑逆转为大额砸盘预警。",
    thinkingMonologue: "多源采集器并发获取 Alternative.me 官方 API 成功... 结合 CryptoPanic 最近 5 条快讯，暂无监管立案或协议暴雷等负面关键词。链上现货买盘真实，情绪面给予积极看多支持。"
  },
  {
    id: "bull",
    name: "BullResearcher",
    title: "多头先锋研究员",
    avatar: "🐂",
    color: "#4D835C",
    tag: "做多先锋 · 目标 $3250",
    badge: "置信度 85%",
    seatPosition: "middle-left",
    stance: "LONG",
    confidence: 0.85,
    roleDescription: "【小白速懂】：在对抗辩论团中担任“反方辩友的死对头”。他的职责是掘地三尺寻找所有支持上涨的理由，计算上涨阻力位和最佳盈利空间，为做多冲锋立论！",
    keyThesis: "当前现货支撑坚实，空头大量止损点堆积在 3,150 美元上方，一旦向上脉冲将引爆一波连环轧空 (Short Squeeze) 暴拉，向上盈亏比高达 2.2:1！",
    evidence: [
      "上方阻力密集区在 3,250 处，与当前现价具备 +4.1% 的确定性上行空间",
      "下方强防守均线位于 3,060，止损风险仅为 -1.9%，盈亏比 2.2:1 极其优异",
      "年化资金费率仅为 +12.4%，做多持仓成本极其低廉，无高额费率磨损",
      "以太坊网络信标链质押量突破历史新高，流通盘进一步紧缩"
    ],
    invalidation: "若跌破 3,060 美元关键防守位，证明空头砸盘力量超出预期，做多立论即刻作废。",
    thinkingMonologue: "根据 Analyst 和 Sentiment 的研报，当前市场属于‘极度恐慌后的首个技术右侧突破’。历史统计显示此类形态在 Hyperliquid 上的胜率超过 68%。我建议坚决做多！"
  },
  {
    id: "bear",
    name: "BearResearcher",
    title: "空头找茬与反转研究员",
    avatar: "🐻",
    color: "#C24D4D",
    tag: "风险找茬 · 防守 $3080",
    badge: "置信度 45%",
    seatPosition: "middle-right",
    stance: "SHORT",
    confidence: 0.45,
    roleDescription: "【小白速懂】：专门给多头“泼冷水、挑刺找茬”的风险对抗专家。他的任务是指出一切可能导致暴跌的陷阱（如上方套牢盘、诱多假突破、费率陷阱），彻底杀死大模型的盲目自信！",
    keyThesis: "上方 3,200 美元是前期震荡平台的密集筹码套牢区，散户解套抛压极其沉重；且美联储降息预期存在反复，严禁盲目追涨！",
    evidence: [
      "日线级别上方 3,200 - 3,240 美元堆积了约 4.8 亿美元的历史套牢筹码",
      "15m K 线上行至 3,130 附近时曾出现细微上影线，说明有短线获利盘试探性出逃",
      "衍生品未平仓量 (OI) 过去 4 小时微增 3%，谨防庄家诱多后向下插针打止损"
    ],
    invalidation: "若 4 小时 K 线实体放量站稳 3,220 美元上方，则套牢盘已被主力资金全部消化，空头观点失效。",
    thinkingMonologue: "虽然多头势头强劲，但我必须履行找茬职责。我认同整体趋势偏多，但坚决反对市价无脑追高，必须要求策略师在 3,060 处严格挂靠止损点，否则我将投否决票。"
  },
  {
    id: "moderator",
    name: "DebateModerator",
    title: "辩论裁判长 (CIO)",
    avatar: "⚖️",
    color: "#DE6438",
    tag: "综合裁决 · 批准发起做多",
    badge: "综合裁定 82%",
    seatPosition: "bottom-center",
    stance: "LONG",
    confidence: 0.82,
    roleDescription: "【小白速懂】：扮演多空激辩的“主审法官”与基金首席投资官 (CIO)。他负责中立权衡多头和空头的辩论论据，消除认知偏差，拍板决定最终买还是卖。",
    keyThesis: "多头论据充分且有真实链上现货提币支撑；空头提出的 3,200 套牢盘风险，已通过将初始止损点严格收紧至 3,060 得到有效对冲。裁决综合结论：批准做多 (LONG)。",
    evidence: [
      "多头置信度 (85%) 远超空头置信度 (45%)",
      "盈亏比达到 2.2:1，严格满足系统最低 1.6:1 准入门槛",
      "吸纳空头研究员建议：绝不追高，采用限价单在 3,120.00 挂单低吸",
      "综合胜率评估 64.5%，期望收益为正"
    ],
    invalidation: "若入场后 30 分钟内未见成交且价格突破 3,140，则撤销原限价单避免被动踏空风险。",
    thinkingMonologue: "双方辩论两轮后观点收敛。多头优势明显，空头的防守诉求已通过严谨的风控止损参数予以采纳。决定授权 StrategyAgent 生成标准化交易意向提案。"
  },
  {
    id: "strategy",
    name: "StrategyAgent",
    title: "定量策略与仓位生成器",
    avatar: "💡",
    color: "#D48B38",
    tag: "半凯利仓位 · 1.5x 杠杆",
    badge: "头寸 $4,680",
    seatPosition: "bottom-left",
    stance: "APPROVE",
    confidence: 0.90,
    roleDescription: "【小白速懂】：数学精算师。根据裁判裁决的方向，结合凯利公式（Kelly Criterion）计算最佳下注金额，精准算出挂单价、止损价、止盈价和杠杆倍数。",
    keyThesis: "当前账户权益 $10,420，根据半凯利风险控制模型，本笔交易划拨 30% 风险资产敞口，使用稳健的 1.5x 杠杆，名义持仓价值 $4,680 USD（约 1.5 ETH）。",
    evidence: [
      "委托方向: BUY (开多 LONG)",
      "委托价格: $3,120.00 (比现价低 $4.50 挂单吃深度)",
      "止损价格: $3,060.00 (潜在最大亏损 $90 USD，占总权益 0.86%)",
      "止盈价格: $3,250.00 (潜在盈利 $195 USD，盈亏比 2.17:1)",
      "分配杠杆: 1.5x (远低于 3.0x 安全天花板)"
    ],
    invalidation: "一旦生成，等待双重风控（RiskOfficer + HardRiskGuard）合规盖章。",
    thinkingMonologue: "正在生成标准 JSON 提案... 提案 ID: prop_20261119_001。校验数据类型完整性完成，向软风控官提交提案审查。"
  },
  {
    id: "risk_officer",
    name: "RiskOfficerAgent",
    title: "第一道：逻辑风控官",
    avatar: "📋",
    color: "#8C7A6B",
    tag: "逻辑闭环 · 盈亏比合规",
    badge: "软风控通过",
    seatPosition: "top-right",
    stance: "APPROVE",
    confidence: 0.95,
    roleDescription: "【小白速懂】：软风控第一道关口。负责检查策略提案是否逻辑矛盾（如做多时止损价居然高于开仓价）、盈亏比是否达到 1.6:1 的最低及格线。",
    keyThesis: "意向提案经数学验证自洽，开仓价 3120 > 止损价 3060，止盈价 3250 > 开仓价 3120，方向与大盘处于上涨态势完全吻合，放行至硬风控。",
    evidence: [
      "盈亏比检查: (3250 - 3120) / (3120 - 3060) = 130 / 60 = 2.17 >= 1.60 (通过)",
      "方向一致性: 策略 LONG 与 Analyst TREND_UP、Moderator LONG 完全一致 (通过)",
      "逻辑互锁: 止损点位落在空头防守位 ($3080) 之下，具有缓冲空间 (通过)"
    ],
    invalidation: "若盈亏比低于 1.6:1，即刻行使软风控驳回权。",
    thinkingMonologue: "对 prop_20261119_001 进行规则解析... 逻辑闭环无自相矛盾现象，数学计算精确无误，核准并盖章放行至第二道硬风控防线。"
  },
  {
    id: "hard_guard",
    name: "HardRiskGuard",
    title: "第二道：七大铁律硬守卫",
    avatar: "🛡️",
    color: "#4D835C",
    tag: "确定性硬防火墙",
    badge: "7/7 全票通过",
    seatPosition: "bottom-right",
    stance: "APPROVE",
    confidence: 1.0,
    roleDescription: "【小白速懂】：底层最铁面无私的代码防火墙！不调用任何大模型，拥有绝对的一票否决权！只要违反当日回撤、杠杆上限、宏观加息避险、插针洗盘任意一条，立刻枪毙提案！",
    keyThesis: "全部通过七大硬风控铁律审查：无日内超额回撤、Bifrost 数据底座价差健康、杠杆合规、单笔敞口未超标、处于宏观大事件避险期之外、无极端插针异动。",
    evidence: [
      "✓ 铁律1 当日回撤: 当前 1.20% < 5.00% 熔断线 (合规)",
      "✓ 铁律2 Bifrost 偏离: Hyperliquid 与 Binance 价差 0.03% 为 HEALTHY (合规)",
      "✓ 铁律3 杠杆天花板: 申请 1.5x <= 3.0x 上限 (合规)",
      "✓ 铁律4 单笔头寸: $4,680 <= $10,420 * 50% (合规)",
      "✓ 铁律5 滑点保护: 申请 0.1% <= 0.3% 容忍线 (合规)",
      "✓ 铁律6 宏观事件避险: 距离最近美国 CPI 还有 3 天，处于静默期外 (合规)",
      "✓ 铁律7 极端插针拦截: 最新 15m K 线振幅 1.1% <= 3.5% (合规)"
    ],
    invalidation: "任何一项铁律违规，毫秒级绝对拒绝，永无例外！",
    thinkingMonologue: "执行毫秒级纯代码布尔值判定... 7 项检查全真 (All True)。提案获得最高执行安全许可，正式授权 PaperExecution 模拟撮合器入场成交！"
  },
  {
    id: "alert_gateway",
    name: "AlertGateway",
    title: "全天候多渠道告警播报员",
    avatar: "🔔",
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
    thinkingMonologue: "订单成交事件已捕获：ETH 开多 1.5 枚 @ 3,120.00 已就绪，准备组装富文本卡片广播至移动端群聊。"
  }
];

/**
 * 初始 30 根 15m 蜡烛线行情数据（用于 K 线图渲染）
 */
export const INITIAL_KLINES = [
  { time: 1710000000, open: 3080.0, high: 3095.0, low: 3075.0, close: 3090.0, volume: 1420 },
  { time: 1710000900, open: 3090.0, high: 3105.0, low: 3085.0, close: 3100.0, volume: 1680 },
  { time: 1710001800, open: 3100.0, high: 3110.0, low: 3095.0, close: 3098.0, volume: 1120 },
  { time: 1710002700, open: 3098.0, high: 3102.0, low: 3088.0, close: 3092.0, volume: 980 },
  { time: 1710003600, open: 3092.0, high: 3096.0, low: 3080.0, close: 3085.0, volume: 1350 },
  { time: 1710004500, open: 3085.0, high: 3090.0, low: 3078.0, close: 3088.0, volume: 1040 },
  { time: 1710005400, open: 3088.0, high: 3104.0, low: 3086.0, close: 3102.0, volume: 1890 },
  { time: 1710006300, open: 3102.0, high: 3115.0, low: 3098.0, close: 3112.0, volume: 2150 },
  { time: 1710007200, open: 3112.0, high: 3122.0, low: 3108.0, close: 3118.0, volume: 1980 },
  { time: 1710008100, open: 3118.0, high: 3125.0, low: 3114.0, close: 3120.0, volume: 1460 },
  { time: 1710009000, open: 3120.0, high: 3128.0, low: 3116.0, close: 3122.0, volume: 1620 },
  { time: 1710009900, open: 3122.0, high: 3132.0, low: 3118.0, close: 3128.0, volume: 2400 },
  { time: 1710010800, open: 3128.0, high: 3135.0, low: 3120.0, close: 3124.5, volume: 1850 },
];
