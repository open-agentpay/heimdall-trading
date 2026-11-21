/**
 * ================================================================================
 * Heimdall 交易平台 - 全球主流加密资产交易标的库 (Mainstream Symbols Registry)
 * ================================================================================
 * 覆盖 Binance / Hyperliquid / OKX 支持的最主流流动性现货与永续合约资产
 */

export const MAINSTREAM_SYMBOLS = [
  { symbol: 'BTC', name: 'Bitcoin (比特币)', category: 'Layer 1 / 大盘', defaultPrice: 96500.0, popular: true },
  { symbol: 'ETH', name: 'Ethereum (以太坊)', category: 'Layer 1 / 智能合约', defaultPrice: 3120.0, popular: true },
  { symbol: 'SOL', name: 'Solana', category: 'High-TPS L1', defaultPrice: 185.0, popular: true },
  { symbol: 'BNB', name: 'BNB (币安币)', category: 'CEX / BNB Chain', defaultPrice: 650.0, popular: true },
  { symbol: 'DOGE', name: 'Dogecoin (狗狗币)', category: 'Meme / 支付', defaultPrice: 0.38, popular: true },
  { symbol: 'XRP', name: 'Ripple (瑞波币)', category: 'Cross-border Payment', defaultPrice: 1.15, popular: true },
  { symbol: 'ADA', name: 'Cardano (艾达币)', category: 'Layer 1 PoS', defaultPrice: 0.75, popular: false },
  { symbol: 'AVAX', name: 'Avalanche (雪崩)', category: 'Layer 1 子网', defaultPrice: 35.0, popular: false },
  { symbol: 'SUI', name: 'Sui Network', category: 'Move 语言高并发 L1', defaultPrice: 3.40, popular: true },
  { symbol: 'LINK', name: 'Chainlink (预言机)', category: 'DeFi 预言机基础设施', defaultPrice: 15.5, popular: false },
  { symbol: 'NEAR', name: 'NEAR Protocol', category: '分片扩容 L1 / AI+Crypto', defaultPrice: 6.20, popular: false },
  { symbol: 'APT', name: 'Aptos', category: 'Move 语言高并发 L1', defaultPrice: 11.8, popular: false },
  { symbol: 'ARB', name: 'Arbitrum', category: 'Ethereum Layer 2 Rollup', defaultPrice: 0.72, popular: false },
  { symbol: 'OP', name: 'Optimism', category: 'Ethereum Layer 2 Superchain', defaultPrice: 1.85, popular: false },
  { symbol: 'PEPE', name: 'Pepe', category: 'Top Meme Token', defaultPrice: 0.000021, popular: false },
];

export const SYMBOL_PRICES = MAINSTREAM_SYMBOLS.reduce((acc, item) => {
  acc[item.symbol] = item.defaultPrice;
  return acc;
}, {});
