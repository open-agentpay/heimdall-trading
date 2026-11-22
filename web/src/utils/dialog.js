/**
 * 统一的提示 / 确认对话框（替代浏览器自带的 alert / confirm）
 * - showAlert(message, opts) → Promise<void>
 * - showConfirm(message, opts) → Promise<boolean>
 * opts: { title, confirmText, cancelText, danger, tone: 'info' | 'warning' | 'error' }
 * 由 <DialogHost /> 渲染（在 main.jsx 挂载）；未挂载时退回浏览器自带对话框。
 */
let seq = 0;
let queue = [];
const listeners = new Set();

const emit = () => {
  const snapshot = [...queue];
  listeners.forEach((fn) => fn(snapshot));
};

export function subscribeDialogs(fn) {
  listeners.add(fn);
  fn([...queue]);
  return () => listeners.delete(fn);
}

export function resolveDialog(id, value) {
  const item = queue.find((d) => d.id === id);
  if (!item) return;
  queue = queue.filter((d) => d.id !== id);
  emit();
  item.resolve(value);
}

function open(kind, message, opts = {}) {
  const text = String(message ?? '');
  if (listeners.size === 0) {
    if (kind === 'confirm') return Promise.resolve(window.confirm(text));
    window.alert(text);
    return Promise.resolve(undefined);
  }
  return new Promise((resolve) => {
    queue = [...queue, { id: ++seq, kind, message: text, ...opts, resolve }];
    emit();
  });
}

export const showAlert = (message, opts) => open('alert', message, opts);
export const showConfirm = (message, opts) => open('confirm', message, opts).then(Boolean);
