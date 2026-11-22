/**
 * ================================================================================
 * Heimdall 交易平台 - 密钥管理安全金库弹窗组件 (KeyVaultModal)
 * ================================================================================
 * 
 * 【小白速懂】：
 * 1. 为什么系统需要存密钥？
 *    - Heimdall 要帮你在交易所（Hyperliquid/币安/OKX）下单执行策略，
 *      且多个 AI Agent（Claude/DeepSeek/GPT-4o）需要调用大模型接口做深度推理，
 *      必须持有相应的 API 凭据。
 * 2. 安全铁律：
 *    - 严禁明文裸奔！所有密钥在后端均通过 AES-256-GCM 加密入库；
 *    - 前端绝不暴露完整密钥，一律采用掩码打码展示（如 sk-ant-•••••••••••4f8e）；
 *    - 针对 Hyperliquid，强烈推荐使用其官方派生的专用【Agent Wallet】私钥，而非主钱包私钥！
 */

import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { showAlert, showConfirm } from '../utils/dialog';
import AddKeyModal from './keyVault/AddKeyModal';
import KeyCard from './keyVault/KeyCard';
import SecurityPanel from './keyVault/SecurityPanel';
import { toVaultKeyView } from './keyVault/vaultKeyView';
import { Key, ShieldCheck, Lock, Plus, Sparkles, Cpu, Building2, HelpCircle, Activity } from 'lucide-react';
import { listVaultKeys, testVaultKey, deleteVaultKey, updateVaultKeyNetwork, activateVaultKey, updateVaultKey } from '../api/endpoints';

function KeyVaultPanel({ isOpen, onClose, walletState, onConnectWallet }) {
  const { t } = useTranslation('keyVault');

  const isWalletConnected = Boolean(walletState?.isConnected && walletState?.address);

  const [activeCategory, setActiveCategory] = useState('exchanges'); // exchanges | llms | security
  const [showAddModal, setShowAddModal] = useState(false);
  const [testingKeyId, setTestingKeyId] = useState(null);
  const [togglingKeyId, setTogglingKeyId] = useState(null);
  const [activatingKeyId, setActivatingKeyId] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  // 编辑已有密钥状态
  const [editingKeyId, setEditingKeyId] = useState(null);
  const [editForm, setEditForm] = useState({
    label: '',
    model: '',
    baseUrl: '',
    apiKey: '',
  });
  const [isSavingEdit, setIsSavingEdit] = useState(false);

  // 密钥列表 (仅属于当前连接钱包的脱敏数据，绝不展示未授权的 Mock 数据)
  const [keysList, setKeysList] = useState([]);

  // 关闭新增弹窗：弹窗卸载后，表单里已输入的密钥随之清除
  const closeAddModal = () => {
    setShowAddModal(false);
  };

  // 异步加载后端密钥 (严格多租户隔离)
  const loadVaultKeys = async () => {
    if (!walletState?.isConnected || !walletState?.address) {
      setKeysList([]);
      return;
    }

    setIsLoading(true);
    try {
      // 以前删除失败时会把凭据编号记在本地并从列表隐藏；列表以后端为准，清掉这份旧记录
      localStorage.removeItem('heimdall_deleted_vault_keys');
      const remoteKeys = await listVaultKeys(walletState.address);
      if (Array.isArray(remoteKeys)) {
        const mapped = remoteKeys.map(toVaultKeyView);
        setKeysList(mapped);
      } else {
        setKeysList([]);
      }
    } catch (e) {
      console.warn("拉取金库密钥失败:", e);
      setKeysList([]);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      if (walletState?.isConnected && walletState?.address) {
        loadVaultKeys();
      } else {
        setKeysList([]);
      }
    }
  }, [isOpen, walletState?.isConnected, walletState?.address]);

  // 测试单项凭据连通性 (真实后端探针)
  const handleTestKey = async (id) => {
    setTestingKeyId(id);
    try {
      const res = await testVaultKey(id);
      const actualLatency = (res && Number.isFinite(Number(res.latency_ms)) && res.latency_ms !== null) ? Math.round(Number(res.latency_ms)) : null;
      const resStatus = (res && res.status) || (res && res.success === false ? 'FAILED' : 'UNKNOWN');
      const resMsg = (res && res.message) || '';
      setKeysList(prev => prev.map(k => {
        if (k.id === id || k.key_id === id) {
          return {
            ...k,
            status: resStatus,
            latency: actualLatency,
            lastTested: t('status.justNow'),
            lastMessage: resMsg,
          };
        }
        return k;
      }));
    } catch (e) {
      setKeysList(prev => prev.map(k => (k.id === id || k.key_id === id) ? { ...k, status: 'FAILED', lastMessage: e.message || t('messages.probeError'), lastTested: t('status.justNow') } : k));
    } finally {
      setTestingKeyId(null);
    }
  };

  // 切换已有币安密钥的网络环境（测试网/模拟盘 <-> 正式网）
  const handleToggleBinanceNetwork = async (keyObj) => {
    const nextIsTestnet = !keyObj.isTestnet;
    setTogglingKeyId(keyObj.id);
    try {
      const res = await updateVaultKeyNetwork(keyObj.id, nextIsTestnet);
      setKeysList(prev => prev.map(k => (k.id === keyObj.id || k.key_id === keyObj.id) ? {
        ...k,
        isTestnet: Boolean(res?.is_testnet ?? nextIsTestnet),
        lastMessage: res?.message || (nextIsTestnet ? t('messages.switchedTestnet') : t('messages.switchedMainnet')),
      } : k));
    } catch (e) {
      showAlert(e.message || t('errors.unknown'), { title: t('alerts.toggleNetworkFailed'), tone: 'error' });
    } finally {
      setTogglingKeyId(null);
    }
  };

  // 激活大模型密钥（多密钥间切换活跃生效密钥）
  const handleActivateKey = async (id, label) => {
    setActivatingKeyId(id);
    try {
      await activateVaultKey(id);
      setKeysList(prev => prev.map(k => {
        if (k.category === 'llms') {
          const isTarget = (k.id === id || k.key_id === id);
          return {
            ...k,
            isActive: isTarget,
            lastMessage: isTarget ? t('messages.activated') : '',
          };
        }
        return k;
      }));
    } catch (e) {
      showAlert(e.message || t('errors.unknown'), { title: t('alerts.activateFailed'), tone: 'error' });
    } finally {
      setActivatingKeyId(null);
    }
  };

  // 开启编辑配置
  const handleStartEdit = (k) => {
    setEditingKeyId(k.id);
    setEditForm({
      label: k.label || '',
      model: k.model || '',
      baseUrl: k.baseUrl || '',
      apiKey: '',
    });
  };

  // 取消编辑
  const handleCancelEdit = () => {
    setEditingKeyId(null);
    // 取消编辑时清空输入过的新密钥
    setEditForm({ label: '', model: '', baseUrl: '', apiKey: '' });
  };

  // 提交保存修改
  const handleSaveEdit = async (keyId) => {
    setIsSavingEdit(true);
    try {
      let rawBaseUrl = editForm.baseUrl.trim();
      for (const suffix of ['/chat/completions', '/chat/completions/', '/completions', '/completions/']) {
        if (rawBaseUrl.endsWith(suffix)) {
          rawBaseUrl = rawBaseUrl.slice(0, -suffix.length);
          break;
        }
      }
      const effectiveBaseUrl = rawBaseUrl.replace(/\/+$/, '');

      const payload = {
        label: editForm.label.trim() || undefined,
        model: editForm.model.trim() || undefined,
        base_url: effectiveBaseUrl,
      };
      if (editForm.apiKey.trim()) {
        payload.api_key = editForm.apiKey.trim();
      }

      const res = await updateVaultKey(keyId, payload);

      setKeysList(prev => prev.map(k => {
        if (k.id === keyId || k.key_id === keyId) {
          return {
            ...k,
            label: res.label || editForm.label,
            model: res.model !== undefined ? res.model : editForm.model,
            baseUrl: res.base_url !== undefined ? res.base_url : effectiveBaseUrl,
            mask: res.key_mask || k.mask,
            lastMessage: t('messages.configUpdated'),
            // 更换了密钥就需要重新测试连通性，不再直接标记为 OK
            ...(payload.api_key ? { status: 'UNTESTED', latency: null, lastTested: t('status.untested') } : {}),
          };
        }
        return k;
      }));
      setEditingKeyId(null);
      setEditForm({ label: '', model: '', baseUrl: '', apiKey: '' });
    } catch (err) {
      showAlert(err.message || t('errors.networkOrAuth'), { title: t('alerts.saveConfigFailed'), tone: 'error' });
    } finally {
      setIsSavingEdit(false);
    }
  };

  // 物理销毁凭据
  const handleDeleteKey = async (id, label) => {
    if (!(await showConfirm(t('alerts.deleteConfirm', { label }), { title: t('alerts.deleteTitle'), confirmText: t('alerts.deleteConfirmText'), danger: true }))) {
      return;
    }

    try {
      await deleteVaultKey(id, walletState?.address);
    } catch (err) {
      // 后端没有删掉就不从列表移除：凭据仍在数据库中，不能显示成"已销毁"
      showAlert(err.message || t('errors.networkOrAuth'), { title: t('alerts.deleteFailed'), tone: 'error' });
      return;
    }
    setKeysList(prev => prev.filter(k => (k.id !== id && k.key_id !== id)));
  };

  // 紧急全局吊销：逐个调用后端删除，按真实结果提示（后端确认删除后才从列表移除）
  const handleRevokeAll = async () => {
    const targets = keysList.map(k => ({ id: k.key_id || k.id, label: k.label || k.key_id || k.id }));
    if (targets.length === 0) {
      showAlert(t('alerts.revokeNone'), { title: t('alerts.revokeNoneTitle') });
      return;
    }
    const ok = await showConfirm(
      t('alerts.revokeConfirm', { n: targets.length }),
      { title: t('alerts.revokeTitle'), confirmText: t('alerts.revokeConfirmText', { n: targets.length }), danger: true }
    );
    if (!ok) return;
    const failed = [];
    for (const target of targets) {
      try {
        await deleteVaultKey(target.id, walletState?.address);
      } catch (err) {
        failed.push(t('alerts.itemFailed', { label: target.label, message: err.message || t('errors.unknown') }));
      }
    }
    await loadVaultKeys();
    if (failed.length === 0) {
      showAlert(t('alerts.revokeDone', { n: targets.length }), { title: t('alerts.revokeDoneTitle') });
    } else {
      showAlert(t('alerts.revokePartial', { deleted: targets.length - failed.length, failed: failed.length, details: failed.join('\n') }), { title: t('alerts.revokePartialTitle'), tone: 'error' });
    }
  };

  // 新凭据入库成功后加入列表；新增的大模型密钥设为激活时，其余大模型密钥取消激活
  const handleKeyCreated = (newKeyObj) => {
    setKeysList(prev => {
      const updated = newKeyObj.category === 'llms' && newKeyObj.isActive
        ? prev.map(k => k.category === 'llms' ? { ...k, isActive: false } : k)
        : prev;
      return [newKeyObj, ...updated];
    });
  };

  // 当前分类下的列表
  const currentFilteredKeys = keysList.filter(k => k.category === activeCategory);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-xs">
      <div 
        className="w-full max-w-3xl bg-panel rounded-2xl border border-line shadow-2xl flex flex-col overflow-hidden max-h-[85vh] animate-in fade-in zoom-in-95 duration-150"
        onClick={(e) => e.stopPropagation()}
      >
        
        {/* 1. 弹窗顶栏 */}
        <div className="px-6 py-4 bg-subtle border-b border-line flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-surface border border-line-strong flex items-center justify-center text-xl shadow-xs">
              <Key className="w-5 h-5 text-brand" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h2 className="text-sm font-bold text-ink">{t('header.title')}</h2>
                <span className="text-xs bg-success-soft text-success border border-success-line px-2 py-0.5 rounded-full font-semibold flex items-center space-x-1">
                  <ShieldCheck className="w-3 h-3" />
                  <span>{t('header.encrypted')}</span>
                </span>
              </div>
              <p className="text-xs text-ink-3 mt-0.5">
                {t('header.subtitle')}
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-2">
            {isWalletConnected ? (
              <button
                onClick={() => setShowAddModal(true)}
                className="px-3 py-1.5 rounded-xl bg-brand hover:bg-brand-hover text-white text-xs font-bold shadow-xs transition flex items-center space-x-1.5"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>{t('header.addKey')}</span>
              </button>
            ) : (
              <span className="px-2.5 py-1 rounded-xl bg-brand-soft border border-brand-line text-brand text-xs font-bold flex items-center space-x-1.5">
                <Lock className="w-3 h-3" />
                <span>{t('header.locked')}</span>
              </span>
            )}
            <button 
              onClick={onClose}
              aria-label={t('header.close')}
              className="w-7 h-7 rounded-lg hover:bg-muted-strong text-ink-3 hover:text-ink flex items-center justify-center text-xs transition"
            >
              ✕
            </button>
          </div>
        </div>

        {!isWalletConnected ? (
          /* 未连接钱包时的多租户强隔离锁定界面 */
          <div className="p-10 flex flex-col items-center justify-center text-center space-y-4 my-auto">
            <div className="w-16 h-16 rounded-2xl bg-brand-soft border border-brand-line text-brand flex items-center justify-center shadow-xs">
              <Lock className="w-8 h-8" />
            </div>
            <div className="space-y-1.5 max-w-md">
              <h3 className="text-base font-bold text-ink">
                {t('locked.title')}
              </h3>
              <p className="text-xs text-ink-3 leading-relaxed">
                {t('locked.desc1')}
              </p>
              <p className="text-xs text-ink-4">
                {t('locked.desc2')}
              </p>
            </div>
            <div className="pt-2">
              <button
                onClick={() => {
                  if (onConnectWallet) onConnectWallet();
                }}
                className="px-5 py-2.5 rounded-xl bg-brand hover:bg-brand-hover text-white text-xs font-bold shadow-sm transition flex items-center space-x-2"
              >
                <ShieldCheck className="w-4 h-4" />
                <span>{t('locked.connect')}</span>
              </button>
            </div>
          </div>
        ) : (
          <>
        {/* 2. 二级分类切换胶囊 */}
        <div className="px-6 pt-3 pb-2 bg-canvas border-b border-line flex items-center justify-between">
          <div className="flex space-x-1.5 bg-muted p-1 rounded-xl">
            <button
              onClick={() => setActiveCategory('exchanges')}
              className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${
                activeCategory === 'exchanges' 
                  ? "bg-surface text-brand shadow-xs" 
                  : "text-ink-3 hover:text-ink"
              }`}
            >
              <Building2 className="w-3.5 h-3.5" />
              <span>{t('tabs.exchanges', { n: keysList.filter(k => k.category === 'exchanges').length })}</span>
            </button>

            <button
              onClick={() => setActiveCategory('llms')}
              className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${
                activeCategory === 'llms' 
                  ? "bg-surface text-brand shadow-xs" 
                  : "text-ink-3 hover:text-ink"
              }`}
            >
              <Cpu className="w-3.5 h-3.5" />
              <span>{t('tabs.llms', { n: keysList.filter(k => k.category === 'llms').length })}</span>
            </button>

            <button
              onClick={() => setActiveCategory('security')}
              className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${
                activeCategory === 'security' 
                  ? "bg-surface text-brand shadow-xs" 
                  : "text-ink-3 hover:text-ink"
              }`}
            >
              <Lock className="w-3.5 h-3.5" />
              <span>{t('tabs.security')}</span>
            </button>
          </div>

          <div className="text-xs text-ink-3 flex items-center space-x-1">
            <Activity className="w-3.5 h-3.5 text-success" />
            <span>{t('tabs.probeStatus')}</span>
          </div>
        </div>

        {/* 3. 弹窗主内容列表 */}
        <div className="p-6 overflow-y-auto space-y-4 flex-1">

          {/* 安全认知提示横幅 */}
          {activeCategory === 'exchanges' && (
            <div className="p-3.5 rounded-xl bg-brand-soft border border-brand-line flex items-start space-x-3 text-xs text-brand-strong">
              <HelpCircle className="w-4 h-4 shrink-0 mt-0.5 text-brand" />
              <div className="leading-relaxed">
                <span className="font-bold">{t('banner.hlTitle')}</span>
                <p className="text-xs text-brand-strong/90 mt-0.5">
                  {t('banner.hlBody')}
                </p>
              </div>
            </div>
          )}

          {activeCategory === 'llms' && (
            <div className="p-3.5 rounded-xl bg-success-soft border border-success-line flex items-start space-x-3 text-xs text-success-strong">
              <Sparkles className="w-4 h-4 shrink-0 mt-0.5 text-success" />
              <div className="leading-relaxed">
                <span className="font-bold">{t('banner.llmTitle')}</span>
                <p className="text-xs text-success-strong/90 mt-0.5">
                  {t('banner.llmBody')}
                </p>
              </div>
            </div>
          )}

          {/* 安全锁面板 */}
          {activeCategory === 'security' && (
            <SecurityPanel onRevokeAll={handleRevokeAll} />
          )}

          {/* 凭据卡片列表 */}
          {activeCategory !== 'security' && (
            <div className="space-y-3">
              {currentFilteredKeys.map(k => (
                <KeyCard
                  key={k.id}
                  k={k}
                  testingKeyId={testingKeyId}
                  togglingKeyId={togglingKeyId}
                  activatingKeyId={activatingKeyId}
                  editingKeyId={editingKeyId}
                  editForm={editForm}
                  setEditForm={setEditForm}
                  isSavingEdit={isSavingEdit}
                  handleTestKey={handleTestKey}
                  handleToggleBinanceNetwork={handleToggleBinanceNetwork}
                  handleActivateKey={handleActivateKey}
                  handleStartEdit={handleStartEdit}
                  handleCancelEdit={handleCancelEdit}
                  handleSaveEdit={handleSaveEdit}
                  handleDeleteKey={handleDeleteKey}
                />
              ))}

              {currentFilteredKeys.length === 0 && (
                <div className="text-center py-10 bg-surface rounded-xl border border-dashed border-line-strong space-y-2">
                  <div className="text-2xl">📭</div>
                  <div className="text-xs font-bold text-ink">{t('empty.title')}</div>
                  <p className="text-xs text-ink-3">{t('empty.hint')}</p>
                </div>
              )}
            </div>
          )}

        </div>
          </>
        )}

        {/* 4. 弹窗底栏 */}
        <div className="px-6 py-3.5 bg-subtle border-t border-line flex items-center justify-between text-xs text-ink-3">
          <span className="flex items-center space-x-1.5">
            <ShieldCheck className="w-3.5 h-3.5 text-success" />
            <span>{t('footer.note')}</span>
          </span>
          <span className="font-mono">Vault Status: SECURE</span>
        </div>

      </div>

      {/* 5. 内层抽屉：添加新凭证弹窗 */}
      {showAddModal && (
        <AddKeyModal
          walletState={walletState}
          isWalletConnected={isWalletConnected}
          onClose={closeAddModal}
          onCreated={handleKeyCreated}
        />
      )}

    </div>
  );
}

/**
 * 弹窗关闭时不渲染。hooks 都在 KeyVaultPanel 里，保证每次渲染的 hooks 调用顺序一致
 * （原来在 hooks 之前 if (!isOpen) return null，不符合 React hooks 规则）；每次打开都是全新状态，与原行为一致。
 */
export default function KeyVaultModal(props) {
  if (!props.isOpen) return null;
  return <KeyVaultPanel {...props} />;
}
