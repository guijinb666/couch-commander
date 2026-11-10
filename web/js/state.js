const STORAGE_KEY = 'remote-control.preferences.v1';
// card_art 是用户提供的整张应用卡片 SVG（306×408，自带渐变底/阴影/品牌图标），铺满卡片显示。
// icon_url 保留为官方站点小图标（本地 PNG，离线可用），用于设置页列表与服务端下发 favicon 的回退。
const DEMO_APPS = [
  { id: 'app-1', name: '抖音', path: 'https://www.douyin.com/', web: 'https://www.douyin.com/', icon: 'tiktok', icon_url: 'assets/icons/douyin.png', card_art: 'assets/icons/douyin.svg', color: '#111118', local: { names: ['抖音'], exe: ['douyin.exe', 'Douyin.exe'], paths: [] } },
  { id: 'app-2', name: 'bilibili', path: 'https://www.bilibili.com/', web: 'https://www.bilibili.com/', icon: 'bilibili', icon_url: 'assets/icons/bilibili.png', card_art: 'assets/icons/bilibili.svg', color: '#FB7299', local: { names: ['bilibili', '哔哩哔哩'], exe: ['bilibili.exe', 'bili.exe'], paths: [] } },
  { id: 'app-3', name: 'QQ音乐', path: 'https://y.qq.com/', web: 'https://y.qq.com/', icon: 'qqmusic', icon_url: 'assets/icons/qqmusic.png', card_art: 'assets/icons/qq_music.svg', color: '#31C27C', local: { names: ['QQ音乐'], exe: ['QQMusic.exe'], paths: [] } },
  { id: 'app-4', name: '网易云音乐', path: 'https://music.163.com/', web: 'https://music.163.com/', icon: 'netease', icon_url: 'assets/icons/netease.png', card_art: 'assets/icons/netease_cloud_music.svg', color: '#C20C0C', local: { names: ['网易云音乐'], exe: ['cloudmusic.exe'], paths: [] } },
  { id: 'app-5', name: '腾讯视频', path: 'https://v.qq.com/', web: 'https://v.qq.com/', icon: 'tencentvideo', icon_url: 'assets/icons/tencentvideo.png', card_art: 'assets/icons/tencent_video.svg', color: '#FF5C00', local: { names: ['腾讯视频'], exe: ['QQLive.exe', 'TencentVideo.exe'], paths: [] } },
];
// 服务端下发的 config.apps 可能不带 card_art，按应用 id 兜底仍使用用户提供的卡片素材。
export const ART_BY_ID = Object.fromEntries(DEMO_APPS.map(app => [app.id, app.card_art]));

export const defaultConfig = () => ({
  version: 1, pages: [], active_page_id: null, hotkeys: [], apps: DEMO_APPS.map(app => ({ ...app })),
  touchpad: { move_sensitivity: 1.6, scroll_speed: 0.6, double_click_ms: 300, drag_hold_ms: 350, lock_hold_ms: 2000 },
});

export class RemoteState extends EventTarget {
  constructor() {
    super();
    this.config = defaultConfig();
    this.preferences = { natural_scroll: false, reverse_swipe: false };
    try {
      this.preferences = { ...this.preferences, ...JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') };
    } catch { /* 隐私模式下仅使用内存配置。 */ }
    this.device = '';
    this.page = 'apps';
    this.selectedApp = 0;
    this.lockedAppId = null;
    this.activePageId = null;
    this.media = { playing: false, title: '', position: 0, best_effort: true };
    this.mediaReceivedAt = 0;
    this.availableApps = [];
    this.connected = false;
  }

  emit(type, detail = {}) { this.dispatchEvent(new CustomEvent(type, { detail })); }
  setConnected(connected) { this.connected = connected; }
  get touchpad() { return this.config.touchpad; }
  get selectedAppData() { return this.config.apps[this.selectedApp] || null; }
  get isLocked() { return Boolean(this.lockedAppId); }
  get activeHotkeys() {
    const pageId = this.activePageId || this.config.active_page_id;
    return this.config.hotkeys.filter(hotkey => hotkey.page_id === pageId);
  }

  setPreference(key, value) {
    this.preferences[key] = value;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(this.preferences)); } catch { /* 保留内存值。 */ }
    this.emit('preferences', this.preferences);
  }

  setConfig(config) {
    if (!config || typeof config !== 'object') return;
    this.config = {
      ...defaultConfig(), ...config,
      touchpad: { ...defaultConfig().touchpad, ...(config.touchpad || {}) },
      apps: Array.isArray(config.apps) && config.apps.length ? config.apps : DEMO_APPS.map(app => ({ ...app })),
      pages: Array.isArray(config.pages) ? config.pages : [],
      hotkeys: Array.isArray(config.hotkeys) ? config.hotkeys : [],
    };
    this.selectedApp = Math.max(0, Math.min(this.selectedApp, this.config.apps.length - 1));
    this.activePageId = this.config.active_page_id || null;
    this.emit('config', this.config);
    this.emit('page-slots');
  }

  selectApp(index) {
    // 锁定后禁止切换应用：点击卡片、横滑、D-pad 左右都走这里，统一拦截；
    // 只有长按中央键（app.release）解锁后才恢复自由切换。
    if (this.isLocked) return;
    const count = this.config.apps.length;
    if (!count) return;
    this.selectedApp = ((index % count) + count) % count;
    this.emit('apps');
  }

  moveApp(direction) { this.selectApp(this.selectedApp + direction); }

  setPage(page) {
    this.page = page;
    this.emit('page', { page });
  }

  setLocked(id) {
    this.lockedAppId = id || null;
    this.emit('lock', { id: this.lockedAppId });
  }

  setMedia(payload) {
    if (typeof payload.playing === 'boolean' && payload.playing !== this.media.playing) {
      this.emit('transport', { playing: payload.playing });
    }
    this.media = { ...this.media, ...payload };
    this.mediaReceivedAt = performance.now();
    this.emit('media', this.media);
  }

  mediaPosition() {
    const base = Math.max(0, Math.min(1, Number(this.media.position) || 0));
    if (!this.media.playing) return base;
    // 有真实时长就用真实时长外推；否则退回 180s 的保守估计（旧行为）。
    const duration = Number(this.media.duration) || 0;
    const window_ms = duration > 0 ? duration * 1000 : 180000;
    return Math.min(1, base + (performance.now() - this.mediaReceivedAt) / window_ms);
  }

  // 前端只按服务端下发的 media 状态走进度；没有视频时本地单击不让进度条自走。
  togglePlay() {}

  update(payload) {
    if (typeof payload.latency_ms === 'number') this.emit('latency', payload.latency_ms);
    if ('locked_app_id' in payload) this.setLocked(payload.locked_app_id);
    if (typeof payload.device === 'string') this.device = payload.device;
    this.emit('status', payload);
  }
}

export function newId(prefix) {
  if (globalThis.crypto?.randomUUID) return `${prefix}-${crypto.randomUUID().slice(0, 8)}`;
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}
