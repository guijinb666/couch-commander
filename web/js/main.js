import { RemoteConnection } from './net.js';
import { RemoteState } from './state.js';
import { AppCarousel } from './app-carousel.js';
import { Dpad } from './dpad.js';
import { Touchpad } from './touchpad.js';
import { Keyboard } from './keyboard.js';
import { Settings } from './settings.js';
import { VoiceButton } from './voice.js';
import { BrightnessControl } from './brightness.js';

const $ = selector => document.querySelector(selector);
const state = new RemoteState();
let toastTimer = 0;
let gestureTimer = 0;

// 初始化阶段出错时，把原因显式暴露到界面与控制台，避免静默白屏。
window.addEventListener('error', event => {
  console.error('[remote] 运行时错误', event.error || event.message);
  const element = document.querySelector('#toast');
  if (element) {
    element.textContent = `初始化失败：${event.message}`;
    element.classList.add('visible');
  }
});
// 早期错误探测钩子（只读，便于定位 PointerEvent / setPointerCapture 兼容性问题）。
window.__earlyErrors = [];
window.addEventListener('error', event => { window.__earlyErrors.push(String(event.message)); });

function toast(text) {
  const element = $('#toast');
  element.textContent = text;
  element.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('visible'), 2200);
}

const connection = new RemoteConnection({
  onMessage(message) {
    const payload = message.payload || {};
    switch (message.type) {
      case 'hello':
        state.device = payload.device || '电脑';
        state.setConnected(true);
        updateConnectionText();
        connection.send('config.get', {});
        connection.send('app.list', {});
        connection.send('media.state', {});
        connection.send('brightness.get', {});
        break;
      case 'status':
        state.update(payload);
        if (typeof payload.latency_ms === 'number') updateLatency(payload.latency_ms);
        break;
      case 'config': state.setConfig(payload.config); break;
      case 'app.list': state.availableApps = Array.isArray(payload.apps) ? payload.apps : []; break;
      case 'media': state.setMedia(payload); break;
      case 'brightness':
        if (typeof payload.value === 'number') brightness.setValue(payload.value);
        break;
      case 'ack':
        if (payload.extra?.best_effort) toast('播放进度由服务端尽力定位');
        if (payload.extra?.route) {
          const route = payload.extra.route;
          const labels = { local: '本地应用', web: '网页版', web_fallback: '已回退网页版' };
          let text = labels[route] || route;
          if (payload.extra.matched_path) text += `：${payload.extra.matched_path}`;
          if (payload.extra.web_fallback) text = `未找到本地应用，${labels.web_fallback}`;
          toast(text);
        }
        break;
      case 'error': toast(payload.message || `命令执行失败（${payload.code || '未知错误'}）`); break;
      default: break;
    }
  },
  onStatus(status, detail) {
    const remote = $('#remote');
    const overlay = $('#disconnect-overlay');
    const pill = $('#connection-pill');
    const connected = status === 'connected';
    const demo = status === 'demo';
    state.setConnected(connected);
    remote.classList.toggle('disconnected', !connected && !demo);
    remote.classList.toggle('is-demo', demo);
    pill.classList.toggle('connected', connected);
    pill.classList.toggle('demo', demo);
    overlay.hidden = connected || demo;
    $('#disconnect-title').textContent = demo ? '演示模式' : '连接已断开';
    $('#disconnect-detail').textContent = demo ? '操作会记录在控制台，不会操作电脑。' : detail || '正在自动重连，也可在设置中手动重连。';
    $('#latency-bars').className = `latency-bars${connected ? '' : ' offline'}`;
    $('#latency-text').textContent = connected ? $('#latency-text').textContent : demo ? '演示' : '离线';
    updateConnectionText();
    if (demo) $('#stage').classList.add('demo-stage');
    if (connected) $('#stage').classList.remove('demo-stage');
  },
  onLatency(ms) { updateLatency(ms); },
});

function updateConnectionText() {
  if (connection.status === 'connected') $('#connection-text').textContent = `已连接 · ${state.device || '电脑'}`;
  else if (connection.status === 'demo') $('#connection-text').textContent = '演示模式';
  else if (connection.status === 'connecting') $('#connection-text').textContent = '连接中…';
  else $('#connection-text').textContent = '已断开';
}

function updateLatency(ms) {
  $('#latency-text').textContent = `${Math.round(ms)}ms`;
  $('#latency-bars').className = `latency-bars${ms > 150 ? ' weak' : ''}`;
}

new AppCarousel($('#app-carousel'), state, { onToast: toast });
new Dpad($('#dpad-zone'), state, connection, { onToast: toast });
const brightness = new BrightnessControl(state, connection, { onToast: toast });
new VoiceButton($('#voice-button'), state, connection, { onToast: toast });
new Touchpad($('#touchpad'), state, connection, {
  onFeedback(text) {
    const feedback = $('#gesture-feedback');
    feedback.textContent = text;
    feedback.classList.add('visible');
    clearTimeout(gestureTimer);
    gestureTimer = setTimeout(() => feedback.classList.remove('visible'), 1000);
  },
});
const settings = new Settings(state, connection, { onToast: toast, onLock: () => updateLock() });
const keyboard = new Keyboard($('#keyboard'), connection, { onClose: () => setKeyboard(false) });

function setKeyboard(open) {
  $('#keyboard-zone').hidden = !open;
  $('#app-zone').hidden = open;
  $('#keyboard-toggle').setAttribute('aria-label', open ? '关闭键盘' : '打开键盘');
  $('#keyboard-toggle').classList.toggle('active', open);
}

function setPage(page) {
  state.setPage(page);
  $('#dpad-zone').hidden = page !== 'apps';
  $('#touchpad-zone').hidden = page !== 'touchpad';
  document.querySelectorAll('[data-page]').forEach(button => {
    const active = button.dataset.page === page;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  if (page !== 'touchpad') setKeyboard(false);
}

document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => setPage(button.dataset.page)));
$('#keyboard-toggle').addEventListener('click', () => setKeyboard($('#keyboard-zone').hidden));
$('#connection-pill').addEventListener('click', () => {
  if (connection.status === 'demo') toast('演示模式：所有待发消息会完整写入控制台');
  else if (connection.status === 'connected') toast(`已连接到 ${state.device || '电脑'}，WebSocket 通道正常`);
  else connection.reconnect();
});
$('#reconnect-button').addEventListener('click', () => connection.reconnect());

function updateLock() {
  $('#play-key').classList.toggle('locked', state.isLocked);
  $('#setting-lock').checked = state.isLocked;
  $('#play-key').setAttribute('aria-label', state.isLocked ? '当前应用已锁定，长按解锁' : '播放或暂停，长按锁定当前应用');
}
state.addEventListener('lock', updateLock);
state.addEventListener('media', () => {
  $('#play-icon use').setAttribute('href', state.media.playing ? '#i-pause' : '#i-play');
});
state.addEventListener('transport', event => {
  if (event.detail.playing) toast('播放');
});

// 把秒数格式化成 mm:ss（超过一小时自动变成 h:mm:ss）。
function formatClock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
  return `${h > 0 ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`;
}

let lastTimeText = '';
function renderTimeLabel(position) {
  const duration = Number(state.media.duration) || 0;
  const label = duration > 0
    ? `${formatClock(position * duration)} / ${formatClock(duration)}`
    : `${formatClock(position * 180)} / --:--`;
  if (label === lastTimeText) return;
  lastTimeText = label;
  const node = $('#progress-time');
  if (node) {
    node.textContent = label;
    node.classList.toggle('playing', Boolean(state.media.playing));
  }
}

function animateProgress() {
  const position = state.mediaPosition();
  $('#progress-fill').style.width = `${position * 100}%`;
  $('#progress-head').style.left = `${position * 100}%`;
  $('#progress-track').setAttribute('aria-valuenow', String(Math.round(position * 100)));
  renderTimeLabel(position);
  requestAnimationFrame(animateProgress);
}
requestAnimationFrame(animateProgress);

// 真实播放进度需要「服务端持续下发」。后端用 SMTC 读到播放器的真实
// position（best_effort=false）后，这里每 1 秒拉一次 media.state，
// 进度条就跟着电脑上正在播的视频走，不再是前端按 180 秒瞎猜。
let mediaPollTimer = 0;
function startMediaPolling() {
  if (mediaPollTimer) return;
  mediaPollTimer = setInterval(() => {
    // 断连时不发（避免堆积），也跳过用户正在拖动进度条的瞬间，
    // 否则服务端回包会把手指位置拽回去。
    if (connection.status !== 'connected') return;
    if (document.hidden) return;
    if (document.body?.dataset.seeking === '1') return;
    connection.send('media.state', {});
  }, 1000);
}
function stopMediaPolling() {
  clearInterval(mediaPollTimer);
  mediaPollTimer = 0;
}
startMediaPolling();
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopMediaPolling();
  else { startMediaPolling(); if (connection.status === 'connected') connection.send('media.state', {}); }
});

// iOS Safari 的双击缩放、双指缩放和页面回弹均不进入遥控手势。
document.addEventListener('gesturestart', event => event.preventDefault());
document.addEventListener('dblclick', event => event.preventDefault(), { passive: false });
let lastTouchEnd = 0;
document.addEventListener('touchend', event => {
  // 只在非交互区域抑制 iOS 双击缩放。对 button / input / 页签等交互元素
  // preventDefault 会吞掉随后合成的 click，正是「A/B/C 快捷键、设置里的
  // 套用模板与键帽点不动」的根因（全局 CSS 的 touch-action:manipulation
  // 已足够防缩放，无需在此拦截交互元素）。
  if (event.target instanceof Element && event.target.closest('button, input, select, textarea, a, label, [role="button"], [role="slider"], [role="progressbar"], .page-slot, .key-cap')) return;
  const now = Date.now();
  if (now - lastTouchEnd < 300) event.preventDefault();
  lastTouchEnd = now;
}, { passive: false });
document.addEventListener('visibilitychange', () => {
  // 回到前台先让连接自检：手机切后台后浏览器既不触发 close 也不改
  // readyState，连接可能已经僵死，表现为「看着连着，点按钮没反应」。
  // resume() 会按「有没有真的收到过帧」判断要不要重建连接。
  // 再兜一层状态机：不是已连接 / 演示就立即重连（覆盖锁屏后 TCP 被系统
  // 断开、以及 connecting 卡住的状态）。
  if (!document.hidden) {
    connection.resume();
    if (['disconnected', 'connecting'].includes(connection.status)) connection.reconnect();
  }
});

// 支持 #touchpad / #keyboard 深链，便于直接进入对应状态（含自测）。
// 同文档内 hash 变更不会重新执行本模块，故同时监听 hashchange 以支持前后切换。
function applyHashRoute() {
  if (location.hash === '#keyboard') {
    setPage('touchpad');
    setKeyboard(true);
  } else if (location.hash === '#touchpad') {
    setPage('touchpad');
    setKeyboard(false);
  } else {
    setPage('apps');
  }
}
applyHashRoute();
window.addEventListener('hashchange', applyHashRoute);
connection.connect();
// 在 file:// 或纯静态服务器打开时仍呈现全部演示控件，不阻断脚本初始化。
if (connection.offline) state.setConfig(state.config);
applyHashRoute();

// 给联调测试提供只读快照入口，不绕过统一消息出口。
Object.defineProperty(window, '__remoteDebug', {
  value: Object.freeze({ snapshot: () => structuredClone({ config: state.config, media: state.media, lockedAppId: state.lockedAppId, status: connection.status }) }),
  writable: false,
});
