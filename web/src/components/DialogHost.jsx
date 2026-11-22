import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Info, XCircle } from 'lucide-react';
import { subscribeDialogs, resolveDialog } from '../utils/dialog';

const TONES = {
  info: { Icon: Info, cls: 'bg-[#E8EFF4] text-[#3E6B89]' },
  warning: { Icon: AlertTriangle, cls: 'bg-[#FBF1E0] text-[#B9770E]' },
  error: { Icon: XCircle, cls: 'bg-[#FBEAEA] text-[#C24D4D]' },
};

/** 渲染 showAlert / showConfirm 排队中的第一个对话框 */
export default function DialogHost() {
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
  const title = current.title || (isConfirm ? '请确认' : '提示');
  const confirmText = current.confirmText || (isConfirm ? '确定' : '知道了');
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
        className="w-full max-w-md rounded-2xl bg-[#FAF7F2] border border-[#E6DFD5] shadow-2xl"
      >
        <div className="px-5 pt-5 flex items-start gap-3">
          <div className={`w-9 h-9 rounded-full flex items-center justify-center shrink-0 ${cls}`}>
            <Icon className="w-4 h-4" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 id="hd-dialog-title" className="text-sm font-bold text-[#2E2823]">{title}</h2>
            <p id="hd-dialog-message" className="mt-1.5 text-[13px] leading-relaxed text-[#5E554B] whitespace-pre-line break-words">
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
              className="px-3.5 py-1.5 rounded-lg border border-[#DCD3C5] bg-white hover:bg-[#F2ECE3] text-xs font-semibold text-[#2E2823] transition focus:outline-none focus:ring-2 focus:ring-[#DE6438]/40"
            >
              {current.cancelText || '取消'}
            </button>
          )}
          <button
            ref={confirmRef}
            type="button"
            onClick={() => close(isConfirm ? true : undefined)}
            className={`px-3.5 py-1.5 rounded-lg text-xs font-bold text-white transition focus:outline-none focus:ring-2 focus:ring-offset-1 ${
              current.danger
                ? 'bg-[#C24D4D] hover:bg-[#A83838] focus:ring-[#C24D4D]/50'
                : 'bg-[#DE6438] hover:bg-[#C8542A] focus:ring-[#DE6438]/50'
            }`}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}
