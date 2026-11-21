/**
 * ================================================================================
 * Heimdall 交易平台 - 短音效与浏览器系统通知工具 (Audio & Web Notifications)
 * ================================================================================
 *
 * 特性：
 * 1. 基于原生 Web Audio API 零外部音频文件合成「成交提示音」与「极端风控/幽灵仓警报音」；
 * 2. 支持全局静音开关（持久化至 localStorage: `heimdall_sound_muted`）；
 * 3. 支持浏览器 Notification API，在后台盯盘时即时推送成交通知与硬风控熔断提醒。
 */

const MUTE_KEY = 'heimdall_sound_muted';

export function isSoundMuted() {
  try {
    return localStorage.getItem(MUTE_KEY) === 'true';
  } catch {
    return false;
  }
}

export function setSoundMuted(muted) {
  try {
    localStorage.setItem(MUTE_KEY, muted ? 'true' : 'false');
  } catch {}
}

function _playTone(freqStart, freqEnd, durationSec, type = 'sine', gainVal = 0.08) {
  if (typeof window === 'undefined' || isSoundMuted()) return;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = type;
    osc.frequency.setValueAtTime(freqStart, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(freqEnd, ctx.currentTime + durationSec);

    gain.gain.setValueAtTime(gainVal, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + durationSec);

    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + durationSec);
    setTimeout(() => ctx.close().catch(() => {}), Math.ceil(durationSec * 1000) + 80);
  } catch (err) {
    console.debug('[Alerts] Web Audio 播放跳过:', err);
  }
}

/** 订单撮合成交时的清脆上扬提示音 */
export function playTradeFillChime() {
  _playTone(587.33, 880.0, 0.18, 'triangle', 0.07);
}

/** 极端风控拦截、幽灵持仓或紧急熔断时的双音警示 */
export function playRiskAlertBeep() {
  _playTone(440.0, 293.66, 0.22, 'sawtooth', 0.06);
  setTimeout(() => _playTone(392.0, 261.63, 0.24, 'sawtooth', 0.06), 240);
}

/** 请求浏览器系统通知权限 */
export async function requestNotificationPermission() {
  if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported';
  if (Notification.permission === 'default') {
    try {
      return await Notification.requestPermission();
    } catch {
      return 'denied';
    }
  }
  return Notification.permission;
}

/** 发送浏览器系统桌面通知 */
export function sendBrowserNotification(title, body) {
  if (typeof window === 'undefined' || !('Notification' in window) || isSoundMuted()) return;
  if (Notification.permission === 'granted') {
    try {
      new Notification(title, { body, silent: true });
    } catch {}
  }
}
