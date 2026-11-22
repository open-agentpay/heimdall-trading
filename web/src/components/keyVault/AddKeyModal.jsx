import React, { useState } from 'react';
import { showAlert } from '../../utils/dialog';
import { Key, Plus } from 'lucide-react';
import { createVaultKey } from '../../api/endpoints';

// 新增表单的空白初始值。弹窗关闭即卸载，已输入的密钥随组件一起清除，不会留在页面状态里
export const EMPTY_NEW_KEY_FORM = {
  category: 'exchanges',
  provider: 'hyperliquid',
  label: '',
  apiKey: '',
  secretKey: '',
  passphrase: '',
  model: '',
  baseUrl: '',
  setActive: true,
  isTestnet: true,
};

/** 登记新凭据弹窗：校验必填项、加密入库，成功后把后端返回的脱敏记录交给父组件 */
export default function AddKeyModal({ walletState, isWalletConnected, onClose, onCreated }) {
  const [newKeyForm, setNewKeyForm] = useState(EMPTY_NEW_KEY_FORM);

  // 提交新增凭证 (真实验密入库)
  const handleSaveNewKey = async (e) => {
    e.preventDefault();
    if (!isWalletConnected) {
      showAlert('请先连接 Web3 钱包，再新增金库凭据。', { title: '未连接钱包', tone: 'warning' });
      return;
    }

    if (!newKeyForm.label) {
      showAlert('请填写自定义别名备注。', { title: '请补全信息', tone: 'warning' });
      return;
    }

    // 校验凭证必填项
    if (newKeyForm.category === 'exchanges') {
      if (newKeyForm.provider === 'hyperliquid') {
        if (!newKeyForm.secretKey) {
          showAlert('请填写 Hyperliquid Agent 派生私钥。', { title: '请补全信息', tone: 'warning' });
          return;
        }
      } else {
        // Binance / OKX / Bybit
        if (!newKeyForm.apiKey || !newKeyForm.secretKey) {
          showAlert(`请同时填写 ${newKeyForm.provider.toUpperCase()} 的 API Key（公钥）与 API Secret（密钥）。`, { title: '请补全信息', tone: 'warning' });
          return;
        }
        if (newKeyForm.provider === 'okx' && !newKeyForm.passphrase) {
          showAlert('欧易 OKX 要求填写 Passphrase（API 密码口令）。', { title: '请补全信息', tone: 'warning' });
          return;
        }
      }
    } else {
      // LLMs
      if (!newKeyForm.apiKey) {
        showAlert('请填写大模型的 API 访问密钥（Key）。', { title: '请补全信息', tone: 'warning' });
        return;
      }
    }

    // 脱敏值以后端返回的 key_mask 为准，前端不再自行截取密钥原文生成掩码
    let createdKeyId = null;
    let createdKeyMask = null;
    try {
      const isBinanceTestnet = newKeyForm.provider === 'binance_demo' ? true : (newKeyForm.provider === 'binance' ? Boolean(newKeyForm.isTestnet) : false);
      // 交易所凭证必须写明市场类型与运行环境，后端按 钱包 + 交易所 + 市场 + 环境 精确匹配
      const EXCHANGE_MARKET = { binance: 'USDM_FUTURES', binance_demo: 'SPOT', hyperliquid: 'USDM_FUTURES' };
      const exchangeScope = EXCHANGE_MARKET[newKeyForm.provider]
        ? {
            market_type: EXCHANGE_MARKET[newKeyForm.provider],
            execution_mode: (newKeyForm.provider === 'binance_demo' || newKeyForm.isTestnet) ? 'TESTNET' : 'MAINNET',
            is_testnet: newKeyForm.provider === 'binance_demo' || Boolean(newKeyForm.isTestnet),
          }
        : {};
      const isLlm = newKeyForm.category === 'llms';
      const effectiveModel = isLlm ? (newKeyForm.model.trim() || (newKeyForm.provider === 'deepseek' ? 'deepseek-chat' : newKeyForm.provider === 'anthropic' ? 'claude-3-5-sonnet-20241022' : 'gpt-4o')) : '';
      let rawBaseUrl = newKeyForm.baseUrl.trim();
      for (const suffix of ['/chat/completions', '/chat/completions/', '/completions', '/completions/']) {
        if (rawBaseUrl.endsWith(suffix)) {
          rawBaseUrl = rawBaseUrl.slice(0, -suffix.length);
          break;
        }
      }
      if (rawBaseUrl) {
        rawBaseUrl = rawBaseUrl.replace(/\/+$/, '') + '/';
      }
      const effectiveBaseUrl = rawBaseUrl;

      const res = await createVaultKey({
        wallet_address: walletState.address,
        category: newKeyForm.category,
        provider: newKeyForm.provider,
        label: newKeyForm.label,
        payload: {
          api_key: newKeyForm.apiKey,
          secret_key: newKeyForm.secretKey,
          private_key: newKeyForm.secretKey,
          passphrase: newKeyForm.passphrase,
          base_url: effectiveBaseUrl,
          ...(isLlm ? { model: effectiveModel, set_active: Boolean(newKeyForm.setActive) } : {}),
          ...(newKeyForm.provider === 'binance' || newKeyForm.provider === 'binance_demo'
            ? { is_testnet: isBinanceTestnet, mode: isBinanceTestnet ? 'demo' : 'mainnet' }
            : {}),
          ...exchangeScope,
        }
      });
      if (res && res.key_id) {
        createdKeyId = res.key_id;
        createdKeyMask = res.key_mask || null;
      }
    } catch (err) {
      console.warn("调用后端入库凭证失败:", err);
      showAlert(err.message || '请确认后端服务正常、钱包登录有效。', { title: '保存凭据失败', tone: 'error' });
      return;
    }

    const actualId = createdKeyId || `key_${Date.now()}`;
    const isLlm = newKeyForm.category === 'llms';
    const isNewActive = isLlm ? Boolean(newKeyForm.setActive) : true;

    const newKeyObj = {
      id: actualId,
      key_id: actualId,
      category: newKeyForm.category,
      provider: newKeyForm.provider,
      providerName: newKeyForm.provider.toUpperCase(),
      label: newKeyForm.label,
      isActive: isNewActive,
      model: isLlm ? (newKeyForm.model.trim() || (newKeyForm.provider === 'deepseek' ? 'deepseek-chat' : newKeyForm.provider === 'anthropic' ? 'claude-3-5-sonnet-20241022' : 'gpt-4o')) : '',
      baseUrl: newKeyForm.baseUrl.trim(),
      mask: createdKeyMask || '••••••••••••••••',
      // 新保存的密钥尚未测试连通性
      status: 'UNTESTED',
      latency: null,
      lastTested: '未测试',
      permissions: ['全权限配置'],
      badge: '新增已加密',
      badgeColor: 'bg-success-soft text-success border-success-line',
      icon: newKeyForm.category === 'exchanges' ? '⚡' : '🧠',
    };

    onCreated(newKeyObj);
    onClose();
  };

  return (
        <div className="fixed inset-0 z-60 flex items-center justify-center p-4 bg-black/50 backdrop-blur-xs animate-in fade-in duration-100">
          <div 
            className="w-full max-w-md bg-panel rounded-2xl border border-line shadow-2xl p-6 space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-line pb-3">
              <h3 className="text-sm font-bold text-ink flex items-center space-x-1.5">
                <Plus className="w-4 h-4 text-brand" />
                <span>登记新交易/大模型凭证</span>
              </h3>
              <button 
                onClick={onClose}
                className="w-6 h-6 rounded-lg hover:bg-muted text-ink-3 flex items-center justify-center text-xs"
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleSaveNewKey} className="space-y-3.5">

              {/* 分类选择 */}
              <div>
                <label className="block text-xs font-semibold text-ink mb-1">凭证类型</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setNewKeyForm(f => ({ ...f, category: 'exchanges', provider: 'hyperliquid' }))}
                    className={`py-2 px-3 rounded-xl border text-xs font-bold transition flex items-center justify-center space-x-1.5 ${
                      newKeyForm.category === 'exchanges' 
                        ? "bg-surface border-brand text-brand shadow-xs" 
                        : "bg-surface/60 border-line text-ink-3"
                    }`}
                  >
                    <span>⚡ 交易所 API / Agent 私钥</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setNewKeyForm(f => ({ ...f, category: 'llms', provider: 'custom' }))}
                    className={`py-2 px-3 rounded-xl border text-xs font-bold transition flex items-center justify-center space-x-1.5 ${
                      newKeyForm.category === 'llms' 
                        ? "bg-surface border-brand text-brand shadow-xs" 
                        : "bg-surface/60 border-line text-ink-3"
                    }`}
                  >
                    <span>🧠 AI 大模型推理 Key</span>
                  </button>
                </div>
              </div>

              {/* 服务商选择 */}
              <div>
                <label className="block text-xs font-semibold text-ink mb-1">目标服务商</label>
                <select
                  value={newKeyForm.provider}
                  onChange={(e) => setNewKeyForm(f => ({ ...f, provider: e.target.value }))}
                  className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs font-medium text-ink focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                >
                  {newKeyForm.category === 'exchanges' ? (
                    <>
                      <option value="hyperliquid">Hyperliquid L1 (Agent 代理私钥)</option>
                      <option value="binance">Binance Futures (币安 U 本位合约 · 官方 SDK 支持实盘与测试网)</option>
                      <option value="binance_demo">Binance Spot Demo (币安现货模拟 demo-api · 仅限现货多头)</option>
                      <option value="okx">OKX (欧易 API Key)</option>
                      <option value="bybit">Bybit 衍生品 API</option>
                    </>
                  ) : (
                    <>
                      <option value="custom">Custom LLM (自定义大模型 · OpenAI 兼容端点)</option>
                      <option value="deepseek">DeepSeek (V3 / R1 推理核心)</option>
                      <option value="openai">OpenAI (GPT-4o / o1)</option>
                      <option value="anthropic">Anthropic (Claude 3.5 Sonnet)</option>
                      <option value="gemini">Google Gemini 2.0</option>
                    </>
                  )}
                </select>
              </div>

              {/* 自定义名称 */}
              <div>
                <label className="block text-xs font-semibold text-ink mb-1">自定义别名备注</label>
                <input
                  type="text"
                  placeholder="如：币安主交易账户 / DeepSeek-V3 核心"
                  value={newKeyForm.label}
                  onChange={(e) => setNewKeyForm(f => ({ ...f, label: e.target.value }))}
                  className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink focus:outline-hidden focus:ring-2 focus:ring-brand/20 font-medium"
                  required
                />
              </div>

              {/* 交易所凭证：Hyperliquid vs 传统交易所 (Binance / OKX / Bybit) */}
              {newKeyForm.category === 'exchanges' && (
                <>
                  {newKeyForm.provider === 'hyperliquid' ? (
                    <>
                      <div>
                        <label className="block text-xs font-semibold text-ink mb-1">
                          Agent 派生私钥 (Private Key) <span className="text-brand">*</span>
                        </label>
                        <input
                          type="password"
                          placeholder="0x... 派生私钥 Hex 字符"
                          value={newKeyForm.secretKey}
                          onChange={(e) => setNewKeyForm(f => ({ ...f, secretKey: e.target.value }))}
                          className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                          required
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-semibold text-ink mb-1">
                          主账户钱包公钥地址 (可选)
                        </label>
                        <input
                          type="text"
                          placeholder="0x... EVM 钱包地址 (选填，留空则由私钥自动推导)"
                          value={newKeyForm.apiKey}
                          onChange={(e) => setNewKeyForm(f => ({ ...f, apiKey: e.target.value }))}
                          className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                        />
                      </div>
                      {newKeyForm.provider === 'hyperliquid' && (
                        <div className="flex items-center justify-between p-2.5 rounded-xl bg-surface border border-line-strong text-xs">
                          <div>
                            <div className="font-semibold text-ink">密钥所属网络环境</div>
                            <div className="text-xs text-ink-3">测试网走 api.hyperliquid-testnet.xyz，正式密钥走 api.hyperliquid.xyz（测试网与正式网的 Agent 私钥不通用）</div>
                          </div>
                          <div className="flex items-center space-x-2 shrink-0 ml-2">
                            <span className={`text-xs font-semibold ${!newKeyForm.isTestnet ? 'text-brand' : 'text-ink-4'}`}>正式</span>
                            <button
                              type="button"
                              role="switch"
                              aria-checked={Boolean(newKeyForm.isTestnet)}
                              onClick={() => setNewKeyForm(f => ({ ...f, isTestnet: !f.isTestnet }))}
                              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors cursor-pointer ${
                                newKeyForm.isTestnet ? 'bg-success-solid' : 'bg-brand'
                              }`}
                            >
                              <span
                                className={`inline-block h-3.5 w-3.5 transform rounded-full bg-surface shadow-xs transition-transform ${
                                  newKeyForm.isTestnet ? 'translate-x-5' : 'translate-x-0.5'
                                }`}
                              />
                            </button>
                            <span className={`text-xs font-semibold ${newKeyForm.isTestnet ? 'text-success' : 'text-ink-4'}`}>测试网</span>
                          </div>
                        </div>
                      )}
                    </>
                  ) : (
                    <>
                      {/* Binance / OKX / Bybit 均需要 API Key (公钥) 与 API Secret (私钥) */}
                      <div>
                        <label className="block text-xs font-semibold text-ink mb-1">
                          API Key (访问公钥) <span className="text-brand">*</span>
                        </label>
                        <input
                          type="text"
                          placeholder={`输入 ${newKeyForm.provider.toUpperCase()} 平台生成的 API Key`}
                          value={newKeyForm.apiKey}
                          onChange={(e) => setNewKeyForm(f => ({ ...f, apiKey: e.target.value }))}
                          className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                          required
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-semibold text-ink mb-1">
                          API Secret (密钥 / 私钥) <span className="text-brand">*</span>
                        </label>
                        <input
                          type="password"
                          placeholder={`输入 ${newKeyForm.provider.toUpperCase()} 对应的 Secret Key`}
                          value={newKeyForm.secretKey}
                          onChange={(e) => setNewKeyForm(f => ({ ...f, secretKey: e.target.value }))}
                          className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                          required
                        />
                      </div>
                      {newKeyForm.provider === 'binance' && (
                        <div className="flex items-center justify-between p-2.5 rounded-xl bg-surface border border-line-strong text-xs">
                          <div>
                            <div className="font-semibold text-ink">密钥所属网络环境</div>
                            <div className="text-xs text-ink-3">测试网/模拟盘走 demo-api.binance.com，正式密钥走 api.binance.com</div>
                          </div>
                          <div className="flex items-center space-x-2 shrink-0 ml-2">
                            <span className={`text-xs font-semibold ${!newKeyForm.isTestnet ? 'text-brand' : 'text-ink-4'}`}>正式</span>
                            <button
                              type="button"
                              role="switch"
                              aria-checked={Boolean(newKeyForm.isTestnet)}
                              onClick={() => setNewKeyForm(f => ({ ...f, isTestnet: !f.isTestnet }))}
                              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors cursor-pointer ${
                                newKeyForm.isTestnet ? 'bg-success-solid' : 'bg-brand'
                              }`}
                            >
                              <span
                                className={`inline-block h-3.5 w-3.5 transform rounded-full bg-surface shadow-xs transition-transform ${
                                  newKeyForm.isTestnet ? 'translate-x-5' : 'translate-x-0.5'
                                }`}
                              />
                            </button>
                            <span className={`text-xs font-semibold ${newKeyForm.isTestnet ? 'text-success' : 'text-ink-4'}`}>测试网</span>
                          </div>
                        </div>
                      )}
                      {newKeyForm.provider === 'okx' && (
                        <div>
                          <label className="block text-xs font-semibold text-ink mb-1">
                            Passphrase (API 密码口令) <span className="text-brand">*</span>
                          </label>
                          <input
                            type="password"
                            placeholder="OKX 独立创建 API 时的 Passphrase 口令"
                            value={newKeyForm.passphrase}
                            onChange={(e) => setNewKeyForm(f => ({ ...f, passphrase: e.target.value }))}
                            className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                            required
                          />
                        </div>
                      )}
                    </>
                  )}
                </>
              )}

              {/* AI 大模型凭证 */}
              {newKeyForm.category === 'llms' && (
                <>
                  <div>
                    <label className="block text-xs font-semibold text-ink mb-1">
                      模型名称 (Model) <span className="text-ink-3 font-normal">(自定义或输入模型代码)</span>
                    </label>
                    <input
                      type="text"
                      placeholder="如：deepseek-chat、deepseek-reasoner、gpt-4o、qwen-plus、claude-3-5-sonnet"
                      value={newKeyForm.model}
                      onChange={(e) => setNewKeyForm(f => ({ ...f, model: e.target.value }))}
                      className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20 font-medium"
                    />
                    <div className="flex items-center space-x-1.5 mt-1 text-xs text-ink-3">
                      <span>推荐快捷填入:</span>
                      <button 
                        type="button" 
                        onClick={() => setNewKeyForm(f => ({ ...f, model: 'deepseek-chat' }))}
                        className="px-1.5 py-0.5 rounded bg-panel border border-line-strong hover:border-brand text-brand"
                      >
                        deepseek-chat
                      </button>
                      <button 
                        type="button" 
                        onClick={() => setNewKeyForm(f => ({ ...f, model: 'gpt-4o' }))}
                        className="px-1.5 py-0.5 rounded bg-panel border border-line-strong hover:border-brand text-brand"
                      >
                        gpt-4o
                      </button>
                      <button 
                        type="button" 
                        onClick={() => setNewKeyForm(f => ({ ...f, model: 'qwen-plus' }))}
                        className="px-1.5 py-0.5 rounded bg-panel border border-line-strong hover:border-brand text-brand"
                      >
                        qwen-plus
                      </button>
                    </div>
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-ink mb-1">
                      API 访问密钥 (API Key) <span className="text-brand">*</span>
                    </label>
                    <input
                      type="password"
                      placeholder="sk-ant-... / sk-... / AIza..."
                      value={newKeyForm.apiKey}
                      onChange={(e) => setNewKeyForm(f => ({ ...f, apiKey: e.target.value }))}
                      className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                      required
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-ink mb-1">
                      API Base URL (自定义端点 / 中转代理 - 选填)
                    </label>
                    <input
                      type="text"
                      placeholder="如：https://openrouter.ai/api/v1、https://api.deepseek.com、http://localhost:11434/v1"
                      value={newKeyForm.baseUrl}
                      onChange={(e) => setNewKeyForm(f => ({ ...f, baseUrl: e.target.value }))}
                      className="w-full p-2.5 rounded-xl bg-surface border border-line-strong text-xs text-ink font-mono focus:outline-hidden focus:ring-2 focus:ring-brand/20"
                    />
                    <div className="flex items-center space-x-1.5 mt-1 text-xs text-ink-3 flex-wrap gap-1">
                      <span>常用端点:</span>
                      <button 
                        type="button" 
                        onClick={() => setNewKeyForm(f => ({ ...f, baseUrl: 'https://openrouter.ai/api/v1' }))}
                        className="px-1.5 py-0.5 rounded bg-panel border border-line-strong hover:border-brand text-brand"
                      >
                        OpenRouter (/v1)
                      </button>
                      <button 
                        type="button" 
                        onClick={() => setNewKeyForm(f => ({ ...f, baseUrl: 'https://api.deepseek.com' }))}
                        className="px-1.5 py-0.5 rounded bg-panel border border-line-strong hover:border-brand text-brand"
                      >
                        DeepSeek
                      </button>
                      <button 
                        type="button" 
                        onClick={() => setNewKeyForm(f => ({ ...f, baseUrl: 'https://api.openai.com/v1' }))}
                        className="px-1.5 py-0.5 rounded bg-panel border border-line-strong hover:border-brand text-brand"
                      >
                        OpenAI
                      </button>
                      <button 
                        type="button" 
                        onClick={() => setNewKeyForm(f => ({ ...f, baseUrl: 'http://localhost:11434/v1' }))}
                        className="px-1.5 py-0.5 rounded bg-panel border border-line-strong hover:border-brand text-brand"
                      >
                        Ollama (本地)
                      </button>
                    </div>
                    <p className="text-xs text-ink-3 mt-0.5">
                      * 提示：标准 OpenAI 兼容端点末尾保留 /v1 即可（系统遵循 RFC 3986 自动规整末尾斜杠，完整保留路径段）
                    </p>
                  </div>
                  <div className="flex items-center space-x-2 p-2.5 bg-surface border border-line-strong rounded-xl">
                    <input
                      type="checkbox"
                      id="setActiveKeyCheck"
                      checked={newKeyForm.setActive}
                      onChange={(e) => setNewKeyForm(f => ({ ...f, setActive: e.target.checked }))}
                      className="w-4 h-4 rounded text-brand focus:ring-brand cursor-pointer"
                    />
                    <label htmlFor="setActiveKeyCheck" className="text-xs font-semibold text-ink cursor-pointer">
                      保存后立即设为当前生效激活密钥 (推演时由多智能体优先调用)
                    </label>
                  </div>
                </>
              )}

              {/* 提交按钮 */}
              <div className="pt-2 flex items-center space-x-2">
                <button
                  type="button"
                  onClick={onClose}
                  className="w-1/2 py-2.5 rounded-xl border border-line-strong hover:bg-subtle text-xs font-semibold text-ink-3 transition"
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="w-1/2 py-2.5 rounded-xl bg-brand hover:bg-brand-hover text-white text-xs font-bold transition shadow-xs"
                >
                  加密存入金库
                </button>
              </div>

            </form>
          </div>
        </div>
  );
}
