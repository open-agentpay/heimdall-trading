import React from 'react';
import { Key, CheckCircle } from 'lucide-react';

/** 密钥金库“安全”标签页：加密方式说明、导出备份（后端暂无接口）、紧急吊销全部凭据 */
export default function SecurityPanel({ onRevokeAll }) {
  return (
        <div className="space-y-4">
          <div className="p-5 rounded-xl bg-surface border border-line shadow-xs space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2.5">
                <div className="w-8 h-8 rounded-lg bg-success-soft text-success flex items-center justify-center font-bold">
                  🛡️
                </div>
                <div>
                  <div className="text-xs font-bold text-ink">金库主加密状态 (Master Key)</div>
                  <div className="text-xs text-ink-3">本地独立 32 字节高熵随机因子已保护入库</div>
                </div>
              </div>
              <span className="text-xs bg-success-soft text-success border border-success-line px-2.5 py-1 rounded-lg font-bold flex items-center space-x-1">
                <CheckCircle className="w-3.5 h-3.5" />
                <span>主动防御中</span>
              </span>
            </div>

            <div className="p-3 rounded-lg bg-panel border border-line-soft text-xs font-mono space-y-1">
              <div className="text-ink-3">加密标准: AES-256-GCM (NIST SP 800-38D)</div>
              <div className="text-ink-3">防重放机制: 12-byte 随机 Nonce + 16-byte MAC 密文校验和</div>
              <div className="text-ink-3">落盘位置: heimdall/data/.vault.key (宿主机权限隔离)</div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="p-4 rounded-xl bg-surface border border-line space-y-2">
              <span className="text-xs font-bold text-ink">导出安全加密备份</span>
              <p className="text-xs text-ink-3">将所有凭据导出为加密备份包。后端还没有导出接口，暂不可用。</p>
              <button
                type="button"
                disabled
                title="后端还没有导出接口，此功能暂不可用"
                className="mt-1 px-3 py-1.5 rounded-lg border border-line-strong text-xs font-semibold text-ink-4 w-full cursor-not-allowed opacity-70"
              >
                暂不支持导出
              </button>
            </div>
            <div className="p-4 rounded-xl bg-surface border border-line space-y-2">
              <span className="text-xs font-bold text-ink">紧急全局吊销 (Revoke All)</span>
              <p className="text-xs text-ink-3">发生潜在泄露时，从数据库删除当前钱包下的全部凭据（不会作废交易所侧的 API Key）。</p>
              <button
                type="button"
                onClick={onRevokeAll}
                className="mt-1 px-3 py-1.5 rounded-lg border border-brand-line bg-brand-soft hover:bg-brand-soft-strong text-xs font-bold text-brand transition w-full"
              >
                粉碎所有凭证
              </button>
            </div>
          </div>
        </div>
  );
}
