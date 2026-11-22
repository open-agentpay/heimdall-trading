import React from 'react';
import { useTranslation } from 'react-i18next';
import { Key, Lock, Trash2, CheckCircle2, RefreshCw, Radio, Pencil, X, Save } from 'lucide-react';

/** 单个凭据卡片：服务商与状态、脱敏值、连通性测试、网络切换 / 激活、编辑与删除 */
export default function KeyCard({
  k,
  testingKeyId,
  togglingKeyId,
  activatingKeyId,
  editingKeyId,
  editForm,
  setEditForm,
  isSavingEdit,
  handleTestKey,
  handleToggleBinanceNetwork,
  handleActivateKey,
  handleStartEdit,
  handleCancelEdit,
  handleSaveEdit,
  handleDeleteKey,
}) {
  const { t } = useTranslation('keyVault');
  // 只展示后端返回的脱敏值；密钥原文不会出现在前端
  const displaySecret = k.mask;
  const isTesting = testingKeyId === k.id;

  return (
    <div 
      key={k.id}
      className="p-4 rounded-xl bg-surface border border-line hover:border-brand/30 hover:shadow-xs transition space-y-3"
    >

      {/* 卡片顶行：服务商、备注名称、状态徽标 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2.5">
          <span className="text-xl">{k.icon}</span>
          <div>
            <div className="flex items-center space-x-2">
              <span className="text-xs font-bold text-ink">{k.label}</span>
              <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${k.badgeColor}`}>
                {k.badge}
              </span>
              {k.category === 'llms' && (
                <span className={`text-xs px-2 py-0.5 rounded-full border font-semibold flex items-center space-x-1 ${
                  k.isActive 
                    ? 'bg-success-soft text-success border-success-line' 
                    : 'bg-subtle text-ink-3 border-line-strong'
                }`}>
                  <span className={`w-1.5 h-1.5 rounded-full ${k.isActive ? 'bg-success-solid animate-pulse' : 'bg-ink-4'}`}></span>
                  <span>{k.isActive ? t('card.active') : t('card.standby')}</span>
                </span>
              )}
            </div>
            <div className="text-xs text-ink-3 mt-0.5 font-mono flex items-center space-x-2">
              <span>{k.providerName}</span>
              {k.category === 'llms' && (
                <span className="bg-panel border border-line px-1.5 py-0.5 rounded text-xs text-brand font-semibold">
                  {t('card.modelTag', { model: k.model || (k.provider === 'deepseek' ? 'deepseek-chat' : k.provider === 'anthropic' ? 'claude-3-5-sonnet' : 'gpt-4o') })}
                </span>
              )}
            </div>
          </div>
        </div>

        {/* 连通性指示器 */}
        <div className="flex items-center space-x-2">
          <div className="flex items-center space-x-1.5 bg-canvas border border-line px-2 py-1 rounded-lg text-xs">
            <span className={`w-2 h-2 rounded-full ${k.status === 'OK' ? 'bg-success-solid' : k.status === 'FAILED' ? 'bg-danger-solid' : k.status === 'WARNING' ? 'bg-warning-solid' : 'bg-ink-4'}`}></span>
            <span className={`font-bold ${k.status === 'OK' ? 'text-success' : k.status === 'FAILED' ? 'text-danger' : k.status === 'WARNING' ? 'text-warning' : 'text-ink-3'}`}>
              {k.status === 'UNTESTED' ? t('status.untested') : k.latency !== null && k.latency !== undefined ? `${k.latency} ms` : k.status}
            </span>
            <span className="text-ink-3">({k.lastTested})</span>
          </div>

          {/* 测试按钮 */}
          <button
            disabled={isTesting}
            onClick={() => handleTestKey(k.id)}
            className="p-1.5 rounded-lg border border-line hover:bg-subtle text-ink-3 hover:text-ink transition"
            title={t('card.testTitle')}
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isTesting ? "animate-spin text-brand" : ""}`} />
          </button>

          {/* 修改配置按钮 */}
          <button
            onClick={() => (editingKeyId === k.id ? handleCancelEdit() : handleStartEdit(k))}
            className={`p-1.5 rounded-lg border transition ${
              editingKeyId === k.id
                ? 'border-brand bg-panel text-brand'
                : 'border-line hover:bg-subtle text-ink-3 hover:text-ink'
            }`}
            title={t('card.editTitle')}
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>

          {/* 删除按钮 */}
          <button
            onClick={() => handleDeleteKey(k.id, k.label)}
            className="p-1.5 rounded-lg border border-brand-line hover:bg-brand-soft text-brand transition"
            title={t('card.deleteTitle')}
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* 编辑表单面板 */}
      {editingKeyId === k.id && (
        <div className="bg-panel p-3 rounded-xl border border-brand/30 space-y-3 animate-in fade-in duration-150">
          <div className="flex items-center justify-between pb-1.5 border-b border-line">
            <span className="text-xs font-bold text-ink flex items-center space-x-1.5">
              <Pencil className="w-3.5 h-3.5 text-brand" />
              <span>{t('card.editHeader')}</span>
            </span>
            <button
              type="button"
              onClick={handleCancelEdit}
              className="text-ink-3 hover:text-ink text-xs cursor-pointer"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
            <div>
              <label className="block text-xs font-semibold text-ink-2 mb-1">{t('add.labelField')}</label>
              <input
                type="text"
                value={editForm.label}
                onChange={(e) => setEditForm(f => ({ ...f, label: e.target.value }))}
                placeholder={t('card.labelPlaceholder')}
                className="w-full p-2 rounded-lg bg-surface border border-line-strong text-xs font-medium text-ink focus:outline-hidden focus:ring-1 focus:ring-brand"
              />
            </div>

            {k.category === 'llms' && (
              <div>
                <label className="block text-xs font-semibold text-ink-2 mb-1">
                  {t('card.modelCode')}
                </label>
                <input
                  type="text"
                  value={editForm.model}
                  onChange={(e) => setEditForm(f => ({ ...f, model: e.target.value }))}
                  placeholder={t('card.modelPlaceholder')}
                  className="w-full p-2 rounded-lg bg-surface border border-line-strong text-xs font-mono font-medium text-ink focus:outline-hidden focus:ring-1 focus:ring-brand"
                />
              </div>
            )}
          </div>

          {k.category === 'llms' && (
            <div className="space-y-1.5">
              <div className="flex items-center space-x-1 text-xs text-ink-3 flex-wrap gap-1">
                <span>{t('card.quickModel')}</span>
                {['deepseek-chat', 'deepseek-reasoner', 'gpt-4o', 'qwen-plus', 'claude-3-5-sonnet'].map(m => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setEditForm(f => ({ ...f, model: m }))}
                    className="px-1.5 py-0.5 rounded bg-surface border border-line-strong hover:border-brand text-brand font-mono cursor-pointer"
                  >
                    {m}
                  </button>
                ))}
              </div>

              <div>
                <label className="block text-xs font-semibold text-ink-2 mb-1">
                  {t('card.baseUrl')}
                </label>
                <input
                  type="text"
                  value={editForm.baseUrl}
                  onChange={(e) => setEditForm(f => ({ ...f, baseUrl: e.target.value }))}
                  placeholder={t('card.baseUrlPlaceholder')}
                  className="w-full p-2 rounded-lg bg-surface border border-line-strong text-xs font-mono font-medium text-ink focus:outline-hidden focus:ring-1 focus:ring-brand"
                />
              </div>
            </div>
          )}

          <div>
            <label className="block text-xs font-semibold text-ink-2 mb-1">
              {t('card.updateKey')} <span className="text-ink-4 font-normal">{t('card.keepHint')}</span>
            </label>
            <input
              type="password"
              value={editForm.apiKey}
              onChange={(e) => setEditForm(f => ({ ...f, apiKey: e.target.value }))}
              placeholder={t('card.newKeyPlaceholder')}
              className="w-full p-2 rounded-lg bg-surface border border-line-strong text-xs font-mono font-medium text-ink focus:outline-hidden focus:ring-1 focus:ring-brand"
            />
          </div>

          <div className="flex items-center justify-end space-x-2 pt-1">
            <button
              type="button"
              onClick={handleCancelEdit}
              className="px-3 py-1.5 rounded-lg border border-line-strong text-ink-2 text-xs hover:bg-surface transition cursor-pointer"
            >
              {t('add.cancel')}
            </button>
            <button
              type="button"
              disabled={isSavingEdit}
              onClick={() => handleSaveEdit(k.id)}
              className="px-3 py-1.5 rounded-lg bg-brand hover:bg-brand-hover text-white text-xs font-bold transition flex items-center space-x-1.5 shadow-xs disabled:opacity-50 cursor-pointer"
            >
              <Save className="w-3.5 h-3.5" />
              <span>{isSavingEdit ? t('card.saving') : t('card.save')}</span>
            </button>
          </div>
        </div>
      )}

      {/* 密钥打码展示槽 */}
      <div className="flex items-center justify-between bg-panel p-2.5 rounded-lg border border-line">
        <div className="flex items-center space-x-2 overflow-hidden">
          <Lock className="w-3.5 h-3.5 text-brand shrink-0" />
          <span className="text-xs font-mono font-bold text-ink truncate">
            {displaySecret}
          </span>
        </div>

        <span className="shrink-0 ml-2 text-xs text-ink-4" title={t('card.maskedTitle')}>
          {t('card.maskedOnly')}
        </span>
      </div>

      {/* 权限清单 + 币安测试网/正式网环境开关 + 大模型激活选择 */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-ink-3">
        <div className="flex items-center space-x-1.5 flex-wrap gap-1">
          <span className="font-semibold text-ink">
            {k.category === 'llms' ? t('card.modelConfig') : t('card.permissions')}
          </span>
          {k.category === 'llms' ? (
            <>
              <span className="bg-panel border border-line px-2 py-0.5 rounded-md font-mono text-brand font-semibold">
                {k.model || (k.provider === 'deepseek' ? 'deepseek-chat' : k.provider === 'anthropic' ? 'claude-3-5-sonnet' : 'gpt-4o')}
              </span>
              {k.baseUrl && (
                <span className="bg-panel border border-line px-2 py-0.5 rounded-md font-mono text-ink-3 max-w-[220px] truncate" title={k.baseUrl}>
                  {k.baseUrl}
                </span>
              )}
            </>
          ) : (
            k.permissions.map((p, idx) => (
              <span key={idx} className="bg-panel border border-line px-2 py-0.5 rounded-md">
                {p}
              </span>
            ))
          )}
        </div>

        {/* 大模型激活选择控制 */}
        {k.category === 'llms' && (
          <div className="flex items-center space-x-2">
            {k.isActive ? (
              <div className="flex items-center space-x-1 bg-success-soft border border-success-line px-2.5 py-1 rounded-lg text-xs text-success font-bold">
                <CheckCircle2 className="w-3.5 h-3.5 text-success" />
                <span>{t('card.activeInEffect')}</span>
              </div>
            ) : (
              <button
                type="button"
                disabled={activatingKeyId === k.id}
                onClick={() => handleActivateKey(k.id, k.label)}
                className="px-2.5 py-1 rounded-lg bg-surface border border-line-strong hover:border-brand hover:text-brand text-ink-2 text-xs font-bold shadow-xs transition flex items-center space-x-1 cursor-pointer disabled:opacity-50"
                title={t('card.activateTitle')}
              >
                <Radio className={`w-3 h-3 ${activatingKeyId === k.id ? 'animate-spin text-brand' : ''}`} />
                <span>{activatingKeyId === k.id ? t('card.switching') : t('card.setActive')}</span>
              </button>
            )}
          </div>
        )}

        {(k.provider === 'binance' || k.provider === 'hyperliquid') && (
          <div className="flex items-center space-x-2 bg-panel border border-line px-2.5 py-1 rounded-lg">
            <span className={`font-semibold ${!k.isTestnet ? 'text-brand' : 'text-ink-4'}`}>
              {t('card.mainnetKey')}
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={Boolean(k.isTestnet)}
              disabled={togglingKeyId === k.id}
              onClick={() => handleToggleBinanceNetwork(k)}
              title={t('card.networkToggleTitle')}
              className={`relative inline-flex h-4 w-8 items-center rounded-full transition-colors cursor-pointer disabled:opacity-50 ${
                k.isTestnet ? 'bg-success-solid' : 'bg-brand'
              }`}
            >
              <span
                className={`inline-block h-3 w-3 transform rounded-full bg-surface shadow-xs transition-transform ${
                  k.isTestnet ? 'translate-x-4' : 'translate-x-0.5'
                }`}
              />
            </button>
            <span className={`font-semibold ${k.isTestnet ? 'text-success' : 'text-ink-4'}`}>
              {t('card.testnetKey')}
            </span>
          </div>
        )}
      </div>

      {k.lastMessage && (
        <div className={`text-xs px-2.5 py-1.5 rounded-lg border ${
          k.status === 'FAILED'
            ? 'bg-danger-soft border-danger-line text-danger'
            : 'bg-success-soft border-success-line text-success-strong'
        }`}>
          {k.lastMessage}
        </div>
      )}

    </div>
  );
}
