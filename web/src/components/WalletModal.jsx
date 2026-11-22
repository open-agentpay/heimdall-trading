/**
 * ================================================================================
 * Heimdall 交易平台 - Web3 钱包登录与去中心化鉴权弹窗 (WalletModal)
 * ================================================================================
 *
 * 【P0 安全对齐】：
 * 1. 真实唤起 MetaMask / OKX 插件读取公钥地址并向后端拉取防重放 Nonce；
 * 2. 强制要求 `POST /api/v1/auth/verify` 真实验签并返回合法 `session_token`，
 *    彻底移除历史伪造签名静默放行逻辑；
 * 3. 未安装插件时仅提供「只读预览模式」（不写入伪造会话 Token），保证多租户写接口零绕过。
 */

import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Wallet, Copy, Check, LogOut, ArrowRight, Loader2
} from 'lucide-react';
import { getAuthNonce, verifySignature, clearAuthSession } from '../api/endpoints';

export default function WalletModal({ isOpen, onClose, walletState, onConnectWallet, onDisconnectWallet }) {
  if (!isOpen) return null;

  const { t } = useTranslation('wallet');
  const [connectingProvider, setConnectingProvider] = useState(null);
  const [copied, setCopied] = useState(false);
  const [activeNetwork] = useState(walletState.network || 'Arbitrum One');
  const [authStep, setAuthStep] = useState('idle');
  const [errorMessage, setErrorMessage] = useState(null);

  useEffect(() => {
    if (typeof window === 'undefined' || !window.ethereum) return;

    const handleAccountsChanged = (accounts) => {
      if (!accounts || accounts.length === 0) {
        handleDisconnect();
      } else if (walletState.isConnected && accounts[0].toLowerCase() !== walletState.address?.toLowerCase()) {
        // 切换钱包账户后必须重新签名换取新账户的 Bearer Token
        clearAuthSession();
        onDisconnectWallet();
      }
    };

    const handleChainChanged = (_chainId) => {
      console.log('[Web3] 链网络变更:', _chainId);
    };

    window.ethereum.on('accountsChanged', handleAccountsChanged);
    window.ethereum.on('chainChanged', handleChainChanged);

    return () => {
      if (window.ethereum.removeListener) {
        window.ethereum.removeListener('accountsChanged', handleAccountsChanged);
        window.ethereum.removeListener('chainChanged', handleChainChanged);
      }
    };
  }, [walletState.isConnected, walletState.address]);

  const walletProviders = [
    {
      id: 'metamask',
      name: t('providers.metamask.name'),
      description: t('providers.metamask.description'),
      icon: '🦊',
      tag: t('providers.metamask.tag'),
      tagColor: 'bg-brand-soft text-brand border-brand-line',
    },
    {
      id: 'okx',
      name: t('providers.okx.name'),
      description: t('providers.okx.description'),
      icon: '⬛',
      tag: t('providers.okx.tag'),
      tagColor: 'bg-success-soft text-success border-success-line',
    },
    {
      id: 'readonly',
      name: t('providers.readonly.name'),
      description: t('providers.readonly.description'),
      icon: '👀',
      tag: t('providers.readonly.tag'),
      tagColor: 'bg-canvas text-ink-3 border-line',
    },
  ];

  const handleCopy = () => {
    if (!walletState.address) return;
    navigator.clipboard.writeText(walletState.address);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleDisconnect = () => {
    clearAuthSession();
    onDisconnectWallet();
    onClose();
  };

  const handleConnect = async (providerId) => {
    setConnectingProvider(providerId);
    setAuthStep('connecting');
    setErrorMessage(null);

    try {
      if (providerId === 'readonly') {
        clearAuthSession();
        setConnectingProvider(null);
        setAuthStep('idle');
        onClose();
        return;
      }

      let ethProvider = null;
      if (providerId === 'metamask' && window.ethereum) {
        ethProvider = window.ethereum;
      } else if (providerId === 'okx' && window.okxwallet) {
        ethProvider = window.okxwallet;
      } else {
        throw new Error(t('errors.noPlugin', { wallet: providerId === 'metamask' ? 'MetaMask' : 'OKX' }));
      }

      const accounts = await ethProvider.request({ method: 'eth_requestAccounts' });
      if (!accounts || accounts.length === 0) {
        throw new Error(t('errors.rejected'));
      }
      const selectedAddress = accounts[0];
      const shortAddr = `${selectedAddress.slice(0, 6)}...${selectedAddress.slice(-4)}`;

      // 步骤 2：向后端获取防重放挑战 Nonce
      setAuthStep('challenge');
      const nonceRes = await getAuthNonce(selectedAddress, 1);
      if (!nonceRes || !nonceRes.message) {
        throw new Error(t('errors.nonceFailed'));
      }

      // 步骤 3：弹出 EIP-191 personal_sign 签名确认框
      setAuthStep('signing');
      const signature = await ethProvider.request({
        method: 'personal_sign',
        params: [nonceRes.message, selectedAddress],
      });

      // 步骤 4：提交后端真实验签，强制校验返回的 Bearer session_token
      setAuthStep('verified');
      const verifyRes = await verifySignature(selectedAddress, signature, nonceRes.message);
      if (!verifyRes || !verifyRes.session_token) {
        throw new Error(t('errors.noSession'));
      }

      localStorage.setItem('heimdall_session_token', verifyRes.session_token);
      localStorage.setItem('heimdall_wallet_address', selectedAddress);

      onConnectWallet({
        address: selectedAddress,
        shortAddress: shortAddr,
        network: activeNetwork,
        provider: providerId,
        connectedAt: new Date().toLocaleTimeString(),
      });

      setTimeout(() => {
        setConnectingProvider(null);
        setAuthStep('idle');
        onClose();
      }, 500);
    } catch (err) {
      console.error('[WalletModal] 钱包连接或验签失败:', err);
      setConnectingProvider(null);
      setAuthStep('error');
      setErrorMessage(err.message || t('errors.signFailed'));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/40 backdrop-blur-xs">
      <div
        className="w-full max-w-lg bg-panel rounded-2xl border border-line shadow-2xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 bg-subtle border-b border-line flex items-center justify-between">
          <div className="flex items-center space-x-2.5">
            <div className="w-9 h-9 rounded-xl bg-surface border border-line-strong flex items-center justify-center shadow-xs">
              <Wallet className="w-5 h-5 text-brand" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-ink flex items-center space-x-2">
                <span>{walletState.isConnected ? t('header.connected') : t('header.connect')}</span>
                {walletState.isConnected && (
                  <span className="text-xs bg-success-soft text-success border border-success-line px-2 py-0.5 rounded-full font-semibold">
                    {t('header.sessionValid')}
                  </span>
                )}
              </h2>
              <p className="text-xs text-ink-3">
                {walletState.isConnected ? t('header.subtitleConnected') : t('header.subtitleConnect')}
              </p>
            </div>
          </div>

          <button
            onClick={onClose}
            className="w-7 h-7 rounded-lg hover:bg-surface text-ink-3 hover:text-ink flex items-center justify-center text-xs font-bold transition"
          >
            ✕
          </button>
        </div>

        {errorMessage && (
          <div className="px-6 py-2.5 bg-danger-soft border-b border-danger-line text-xs font-semibold text-danger flex items-center justify-between">
            <span className="truncate pr-2">{errorMessage}</span>
            <button onClick={() => setErrorMessage(null)} className="text-xs underline shrink-0">{t('dismiss')}</button>
          </div>
        )}

        <div className="p-6 space-y-5">
          {walletState.isConnected ? (
            <div className="space-y-4">
              <div className="p-4 bg-surface rounded-xl border border-line-strong space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-ink-3 font-semibold">{t('activeAddress')}</span>
                  <span className="text-xs font-mono text-success bg-success-soft px-2 py-0.5 rounded">
                    {walletState.network || 'Arbitrum One'}
                  </span>
                </div>

                <div className="flex items-center justify-between bg-canvas p-3 rounded-lg border border-line">
                  <span className="font-mono text-xs text-ink font-bold select-all truncate pr-2">
                    {walletState.address}
                  </span>
                  <button
                    onClick={handleCopy}
                    className="p-1.5 hover:bg-surface rounded-md text-ink-3 hover:text-brand transition"
                  >
                    {copied ? <Check className="w-4 h-4 text-success" /> : <Copy className="w-4 h-4" />}
                  </button>
                </div>
              </div>

              <button
                onClick={handleDisconnect}
                className="w-full py-2.5 bg-panel hover:bg-brand-soft text-brand hover:text-danger text-xs font-bold rounded-xl border border-brand-line flex items-center justify-center space-x-2 transition"
              >
                <LogOut className="w-4 h-4" />
                <span>{t('disconnect')}</span>
              </button>
            </div>
          ) : (
            <div className="space-y-2.5">
              {walletProviders.map((prov) => {
                const isCurrent = connectingProvider === prov.id;
                return (
                  <div
                    key={prov.id}
                    onClick={() => !connectingProvider && handleConnect(prov.id)}
                    className="p-3.5 bg-surface hover:bg-panel rounded-xl border border-line-strong hover:border-brand cursor-pointer transition flex items-center justify-between"
                  >
                    <div className="flex items-center space-x-3">
                      <span className="text-2xl">{prov.icon}</span>
                      <div>
                        <div className="flex items-center space-x-2">
                          <span className="text-xs font-bold text-ink">{prov.name}</span>
                          <span className={`text-xs px-1.5 py-0.2 rounded border font-semibold ${prov.tagColor}`}>
                            {prov.tag}
                          </span>
                        </div>
                        <p className="text-xs text-ink-3 mt-0.5">{prov.description}</p>
                      </div>
                    </div>

                    <div>
                      {isCurrent ? (
                        <div className="flex items-center space-x-1.5 text-xs text-brand font-bold">
                          <Loader2 className="w-4 h-4 animate-spin" />
                          <span>
                            {authStep === 'connecting' && t('steps.connecting')}
                            {authStep === 'challenge' && t('steps.challenge')}
                            {authStep === 'signing' && t('steps.signing')}
                            {authStep === 'verified' && t('steps.verified')}
                          </span>
                        </div>
                      ) : (
                        <ArrowRight className="w-4 h-4 text-ink-4" />
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
