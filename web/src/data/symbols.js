/**
 * ================================================================================
 * Heimdall 交易平台 - 全球主流加密资产交易标的库 (Mainstream Symbols Registry)
 * ================================================================================
 * 覆盖 Binance / Hyperliquid / OKX 支持的最主流流动性现货与永续合约资产
 */

import i18n from '../i18n';

// name / category 是 getter：每次读取时按当前语言从 symbols 语言包取文字，切换语言后自动更新
const withText = (item) => ({
  ...item,
  get name() {
    return i18n.t(`symbols:${item.symbol.toLowerCase()}.name`);
  },
  get category() {
    return i18n.t(`symbols:${item.symbol.toLowerCase()}.category`);
  },
});

export const MAINSTREAM_SYMBOLS = [
  withText({ symbol: 'BTC', defaultPrice: 96500.0, popular: true }),
  withText({ symbol: 'ETH', defaultPrice: 3120.0, popular: true }),
  withText({ symbol: 'SOL', defaultPrice: 185.0, popular: true }),
  withText({ symbol: 'BNB', defaultPrice: 650.0, popular: true }),
  withText({ symbol: 'DOGE', defaultPrice: 0.38, popular: true }),
  withText({ symbol: 'XRP', defaultPrice: 1.15, popular: true }),
  withText({ symbol: 'ADA', defaultPrice: 0.75, popular: false }),
  withText({ symbol: 'AVAX', defaultPrice: 35.0, popular: false }),
  withText({ symbol: 'SUI', defaultPrice: 3.40, popular: true }),
  withText({ symbol: 'LINK', defaultPrice: 15.5, popular: false }),
  withText({ symbol: 'NEAR', defaultPrice: 6.20, popular: false }),
  withText({ symbol: 'APT', defaultPrice: 11.8, popular: false }),
  withText({ symbol: 'ARB', defaultPrice: 0.72, popular: false }),
  withText({ symbol: 'OP', defaultPrice: 1.85, popular: false }),
  withText({ symbol: 'PEPE', defaultPrice: 0.000021, popular: false }),
];

export const SYMBOL_PRICES = MAINSTREAM_SYMBOLS.reduce((acc, item) => {
  acc[item.symbol] = item.defaultPrice;
  return acc;
}, {});
