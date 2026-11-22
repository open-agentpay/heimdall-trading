import React from 'react';
import { useTranslation } from 'react-i18next';
import { Key, CheckCircle } from 'lucide-react';

/** 密钥金库“安全”标签页：加密方式说明、导出备份（后端暂无接口）、紧急吊销全部凭据 */
export default function SecurityPanel({ onRevokeAll }) {
  const { t } = useTranslation('keyVault');
  return (
        <div className="space-y-4">
          <div className="p-5 rounded-xl bg-surface border border-line shadow-xs space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2.5">
                <div className="w-8 h-8 rounded-lg bg-success-soft text-success flex items-center justify-center font-bold">
                  🛡️
                </div>
                <div>
                  <div className="text-xs font-bold text-ink">{t('security.masterTitle')}</div>
                  <div className="text-xs text-ink-3">{t('security.masterDesc')}</div>
                </div>
              </div>
              <span className="text-xs bg-success-soft text-success border border-success-line px-2.5 py-1 rounded-lg font-bold flex items-center space-x-1">
                <CheckCircle className="w-3.5 h-3.5" />
                <span>{t('security.defending')}</span>
              </span>
            </div>

            <div className="p-3 rounded-lg bg-panel border border-line-soft text-xs font-mono space-y-1">
              <div className="text-ink-3">{t('security.encStandard')}</div>
              <div className="text-ink-3">{t('security.antiReplay')}</div>
              <div className="text-ink-3">{t('security.storage')}</div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="p-4 rounded-xl bg-surface border border-line space-y-2">
              <span className="text-xs font-bold text-ink">{t('security.exportTitle')}</span>
              <p className="text-xs text-ink-3">{t('security.exportDesc')}</p>
              <button
                type="button"
                disabled
                title={t('security.exportDisabledTitle')}
                className="mt-1 px-3 py-1.5 rounded-lg border border-line-strong text-xs font-semibold text-ink-4 w-full cursor-not-allowed opacity-70"
              >
                {t('security.exportBtn')}
              </button>
            </div>
            <div className="p-4 rounded-xl bg-surface border border-line space-y-2">
              <span className="text-xs font-bold text-ink">{t('security.revokeTitle')}</span>
              <p className="text-xs text-ink-3">{t('security.revokeDesc')}</p>
              <button
                type="button"
                onClick={onRevokeAll}
                className="mt-1 px-3 py-1.5 rounded-lg border border-brand-line bg-brand-soft hover:bg-brand-soft-strong text-xs font-bold text-brand transition w-full"
              >
                {t('security.revokeBtn')}
              </button>
            </div>
          </div>
        </div>
  );
}
