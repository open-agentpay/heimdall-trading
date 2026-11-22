import { useCallback, useEffect, useRef, useState } from 'react';
import { clearAuthSession } from '../api/endpoints';

export const DISCONNECTED_WALLET = {
  isConnected: false,
  address: '',
  shortAddress: '',
  network: 'Arbitrum One',
  balance: '',
  provider: '',
};

/**
 * 钱包登录状态：从本地存储恢复、连接 / 断开时持久化，并监听接口返回 401 时派发的 heimdall:auth-expired 事件。
 * showToast 用于提示；onAuthExpired 在会话过期时调用（App 用它唤起钱包签名弹窗）。
 */
export function useWalletSession({ showToast, onAuthExpired } = {}) {
  const [walletState, setWalletState] = useState(() => {
    const isConn = localStorage.getItem("heimdall_wallet_connected") === "true";
    if (isConn) {
      try {
        const saved = localStorage.getItem("heimdall_wallet_state");
        if (saved) return JSON.parse(saved);
      } catch (e) {}
    }
    return { ...DISCONNECTED_WALLET };
  });

  // 事件监听只注册一次，回调通过 ref 取最新值
  const callbacksRef = useRef({ showToast, onAuthExpired });
  callbacksRef.current = { showToast, onAuthExpired };

  useEffect(() => {
    const onExpired = (evt) => {
      setWalletState({ ...DISCONNECTED_WALLET });
      callbacksRef.current.showToast?.(`🔐 ${evt?.detail?.message || '会话已过期，请重新连接钱包签名登录'}`, 'critical');
      callbacksRef.current.onAuthExpired?.();
    };
    window.addEventListener('heimdall:auth-expired', onExpired);
    return () => window.removeEventListener('heimdall:auth-expired', onExpired);
  }, []);

  // 钱包连接成功（持久化到本地存储）
  const handleConnectWallet = useCallback((newWalletData) => {
    const fullState = { isConnected: true, ...newWalletData };
    setWalletState(fullState);
    localStorage.setItem('heimdall_wallet_connected', 'true');
    localStorage.setItem('heimdall_wallet_state', JSON.stringify(fullState));
    if (newWalletData.address) {
      localStorage.setItem('heimdall_wallet_address', newWalletData.address);
    }
    callbacksRef.current.showToast?.(`🎉 钱包连接成功！已授权: ${newWalletData.shortAddress || newWalletData.address}`, 'success');
  }, []);

  // 断开连接（清空持久化状态与登录会话）
  const handleDisconnectWallet = useCallback(() => {
    setWalletState({ ...DISCONNECTED_WALLET });
    localStorage.removeItem('heimdall_wallet_connected');
    localStorage.removeItem('heimdall_wallet_state');
    clearAuthSession();
    callbacksRef.current.showToast?.('👋 钱包已安全断开连接', 'info');
  }, []);

  return { walletState, handleConnectWallet, handleDisconnectWallet };
}
