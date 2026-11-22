import React from 'react';

/** 右下角操作反馈浮窗（notification 由 useToast 提供） */
export default function Toast({ notification }) {
  if (!notification) return null;
  return (
    <div 
      className={`fixed bottom-6 right-6 z-50 px-4 py-2.5 rounded-xl shadow-lg border text-xs font-semibold flex items-center space-x-2 animate-bounce ${
        notification.type === "critical"
          ? "bg-danger-solid text-white border-danger"
          : notification.type === "success"
          ? "bg-success-solid text-white border-success"
          : "bg-panel text-ink border-line-strong"
      }`}
    >
      <span>{notification.msg}</span>
    </div>
  );
}
