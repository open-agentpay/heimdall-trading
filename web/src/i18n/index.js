/**
 * 前端多语言：i18next + react-i18next（标准用法）
 *
 * 语言包：./locales/<语言>/<命名空间>.json，一个页面或一组组件一个命名空间（common、nav、arena …）。
 *   新增命名空间只要放进两个语言目录，下面的 import.meta.glob 会自动加载，不用改这个文件。
 * 组件里：
 *   const { t } = useTranslation('nav');      // 默认命名空间
 *   t('order.submit')                          // nav 命名空间
 *   t('common:refresh')                        // 其他命名空间用 "命名空间:键"
 *   t('metrics.tradeCount', { count: 3 })      // 插值：语言包里写 {{count}}
 *   <Trans t={t} i18nKey="order.freeCashLine" values={{ amount }} components={{ strong: <strong /> }} />
 *                                              // 句子里夹着标签时用 Trans，语言包里写 <strong>{{amount}}</strong>
 * 组件以外（纯函数、接口封装）：import i18n from '../i18n';  i18n.t('nav:priceSource.live')
 * 数字、金额、时间：import { fmt } from '../i18n';  fmt.usd(x) / fmt.price(x) / fmt.pct(x) / fmt.dateTime(x)
 *
 * 语言选择：浏览器本地存储 heimdall_locale → 浏览器语言 → 简体中文；切换后同步到 <html lang>。
 */
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

export const DEFAULT_LOCALE = 'zh-CN';
/** 语言名称用各自语言书写（切换按钮上显示） */
export const LANGUAGES = {
  'zh-CN': { label: '简体中文', short: '中' }, // i18n-ignore 语言名称用该语言本身书写
  'en-US': { label: 'English', short: 'EN' },
};
export const SUPPORTED_LOCALES = Object.keys(LANGUAGES);

// ./locales/zh-CN/nav.json -> resources['zh-CN'].nav
const modules = import.meta.glob('./locales/*/*.json', { eager: true, import: 'default' });
const resources = {};
const namespaces = new Set();
Object.entries(modules).forEach(([path, messages]) => {
  const m = path.match(/\.\/locales\/([^/]+)\/([^/]+)\.json$/);
  if (!m) return;
  const [, lng, ns] = m;
  resources[lng] = resources[lng] || {};
  resources[lng][ns] = messages;
  namespaces.add(ns);
});

/** zh、zh-Hans、zh-TW → zh-CN；en、en-GB → en-US；其他语言交给 fallbackLng */
export function normalizeLocale(lng) {
  const s = String(lng || '').toLowerCase();
  if (s.startsWith('zh')) return 'zh-CN';
  if (s.startsWith('en')) return 'en-US';
  return lng;
}

const isDev = typeof import.meta !== 'undefined' && Boolean(import.meta.env && import.meta.env.DEV);

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    ns: [...namespaces],
    defaultNS: 'common',
    fallbackLng: DEFAULT_LOCALE,
    supportedLngs: SUPPORTED_LOCALES,
    load: 'currentOnly',
    initAsync: false,
    interpolation: { escapeValue: false }, // React 已经做了转义
    react: { useSuspense: false },          // 语言包已打包进来，不需要 Suspense
    detection: {
      order: ['localStorage', 'navigator'],
      lookupLocalStorage: 'heimdall_locale',
      caches: ['localStorage'],
      convertDetectedLanguage: normalizeLocale,
    },
    saveMissing: isDev,
    missingKeyHandler: (lngs, ns, key) => {
      console.warn(`[i18n] 缺少文案: ${ns}:${key} (${lngs.join(', ')})`);
    },
  });

const syncHtmlLang = (lng) => {
  if (typeof document !== 'undefined' && lng) document.documentElement.lang = lng;
};
syncHtmlLang(i18n.resolvedLanguage || i18n.language);
i18n.on('languageChanged', syncHtmlLang);

/** 当前语言（zh-CN / en-US） */
export const getLocale = () => i18n.resolvedLanguage || i18n.language || DEFAULT_LOCALE;

// ------------------------------------------------------------------ 格式化（按当前语言，缺失值统一显示 “--”）
const toNum = (value) => {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

export const fmt = {
  /** 固定小数位、带千分位 */
  number(value, digits = 2) {
    const n = toNum(value);
    return n === null ? '--' : n.toLocaleString(getLocale(), { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  /** 美元金额；signed 时正数带 +，负号放在 $ 前（-$1.23） */
  usd(value, digits = 2, { signed = false } = {}) {
    const n = toNum(value);
    if (n === null) return '--';
    const body = `$${Math.abs(n).toLocaleString(getLocale(), { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
    if (n < 0) return `-${body}`;
    return signed && n > 0 ? `+${body}` : body;
  },
  /** 价格：至少 2 位小数，低价币最多显示 8 位 */
  price(value) {
    const n = toNum(value);
    return n === null ? '--' : `$${n.toLocaleString(getLocale(), { minimumFractionDigits: 2, maximumFractionDigits: 8 })}`;
  },
  /** 小数比例转百分比（0.0123 -> 1.23%） */
  pct(fraction, digits = 2) {
    const n = toNum(fraction);
    return n === null ? '--' : `${(n * 100).toLocaleString(getLocale(), { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
  },
  /**
   * 时间：按浏览器本地时区显示，并带时区缩写。
   * 后端不带时区的时间（如 2026-11-22 04:09:52）按 UTC 解析；解析不了时原样返回。
   */
  dateTime(value) {
    if (value === null || value === undefined || value === '') return '--';
    let d;
    if (typeof value === 'number') d = new Date(value);
    else {
      const s = String(value).trim();
      const noZone = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s);
      d = new Date(noZone ? `${s.replace(' ', 'T')}Z` : s);
    }
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat(getLocale(), {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZoneName: 'short',
    }).format(d);
  },
  /** 只显示时分秒（本地时区） */
  time(value) {
    if (value === null || value === undefined || value === '') return '--';
    const d = new Date(typeof value === 'number' ? value : String(value));
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat(getLocale(), { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(d);
  },
};

export default i18n;
