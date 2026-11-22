import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Info, XCircle } from 'lucide-react';
import { subscribeDialogs, resolveDialog } from '../utils/dialog';

const TONES = {
  info: { Icon: Info, cls: 'bg-info-soft text-info' },
  warning: { Icon: AlertTriangle, cls: 'bg-warning-soft text-warning' },
  error: { Icon: XCircle, cls: 'bg-danger-soft text-danger' },
};

/** 渲染 showAlert / showConfirm 排队中的第一个对话框 */
export default function DialogHost() {
  const { t } = useTranslation('dialog');
  const [queue, setQueue] = useState([]);
  const confirmRef = useRef(null);
  const cancelRef = useRef(null);
  const current = queue[0];

  useEffect(() => subscribeDialogs(setQueue), []);

  useEffect(() => {
    if (!current) return undefined;
    // 危险操作默认聚焦"取消"，避免误按回车直接执行
    const target = current.kind === 'confirm' && current.danger ? cancelRef.current : confirmRef.current;
    target?.focus();
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        resolveDialog(current.id, current.kind === 'confirm' ? false : undefined);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [current]);

  if (!current) return null;

  const isConfirm = current.kind === 'confirm';
  const tone = current.tone || (current.danger ? 'warning' : isConfirm ? 'warning' : 'info');
  const { Icon, cls } = TONES[tone] || TONES.info;
  const title = current.title || (isConfirm ? t('titleConfirm') : t('titleAlert'));
  const confirmText = current.confirmText || (isConfirm ? t('ok') : t('gotIt'));
  const close = (value) => resolveDialog(current.id, value);

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-[#2E2823]/40 backdrop-blur-[2px]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !isConfirm) close(undefined);
      }}
    >
      <div
        role={isConfirm ? 'alertdialog' : 'dialog'}
        aria-modal="true"
        aria-labelledby="hd-dialog-title"
        aria-describedby="hd-dialog-message"
        className="w-full max-w-md rounded-2xl bg-panel border border-line shadow-2xl"
      >
        <div className="px-5 pt-5 flex items-start gap-3">
          <div className={`w-9 h-9 rounded-full flex items-center justify-center shrink-0 ${cls}`}>
            <Icon className="w-4 h-4" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 id="hd-dialog-title" className="text-sm font-bold text-ink">{title}</h2>
            <p id="hd-dialog-message" className="mt-1.5 text-[13px] leading-relaxed text-ink-2 whitespace-pre-line break-words">
              {current.message}
            </p>
          </div>
        </div>
        <div className="px-5 py-4 mt-1 flex justify-end gap-2">
          {isConfirm && (
            <button
              ref={cancelRef}
              type="button"
              onClick={() => close(false)}
              className="px-3.5 py-1.5 rounded-lg border border-line-strong bg-surface hover:bg-subtle text-xs font-semibold text-ink transition focus:outline-none focus:ring-2 focus:ring-brand/40"
            >
              {current.cancelText || t('cancel')}
            </button>
          )}
          <button
            ref={confirmRef}
            type="button"
            onClick={() => close(isConfirm ? true : undefined)}
            className={`px-3.5 py-1.5 rounded-lg text-xs font-bold text-white transition focus:outline-none focus:ring-2 focus:ring-offset-1 ${
              current.danger
                ? 'bg-danger-solid hover:bg-danger-solid-hover focus:ring-danger/50'
                : 'bg-brand hover:bg-brand-hover focus:ring-brand/50'
            }`}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}
