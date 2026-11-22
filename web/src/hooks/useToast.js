import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * 全局操作反馈提示（右下角浮窗）。
 * 新提示会重新计时（原实现每条提示各自 setTimeout，前一条的计时器会提前关掉后一条）。
 */
export function useToast(durationMs = 3500) {
  const [notification, setNotification] = useState(null);
  const timerRef = useRef(null);

  const showToast = useCallback((msg, type = 'info') => {
    setNotification({ msg, type });
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setNotification(null), durationMs);
  }, [durationMs]);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  return { notification, showToast };
}
