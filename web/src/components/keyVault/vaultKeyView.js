import i18n, { fmt } from '../../i18n';

/** 把后端返回的脱敏凭据记录转换成密钥金库列表里的展示对象（只用后端给出的脱敏值，不接触密钥原文） */
export function toVaultKeyView(k) {
  const actualId = k.key_id || k.id;
  const prov = String(k.provider || '').toLowerCase();
  const isTestnet = prov === 'binance_demo' ? true : Boolean(k.is_testnet);
  const isActive = Boolean(k.is_active ?? true);
  return {
    id: actualId,
    key_id: actualId,
    category: k.category,
    provider: prov,
    providerName: prov.toUpperCase(),
    isTestnet,
    isActive,
    model: k.model || '',
    baseUrl: k.base_url || '',
    label: k.label,
    mask: k.key_mask || k.mask || '••••••••••••••••',
    // 从未测试过的密钥标记为 UNTESTED，不再默认显示 OK / 80ms
    status: k.last_test_status || 'UNTESTED',
    lastMessage: '',
    latency: Number.isFinite(Number(k.last_test_latency_ms)) && k.last_test_latency_ms !== null ? Math.round(Number(k.last_test_latency_ms)) : null,
    // 文字字段在转换时按当前语言翻译
    lastTested: k.last_tested_at ? fmt.dateTime(k.last_tested_at) : i18n.t('keyVault:status.untested'),
    permissions: [i18n.t('keyVault:card.fullPermissions')],
    badge: i18n.t('keyVault:badge.encrypted'),
    badgeColor: 'bg-success-soft text-success border-success-line',
    icon: k.category === 'exchanges' ? '⚡' : '🧠',
  };
}
