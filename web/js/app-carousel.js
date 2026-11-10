import { ART_BY_ID } from './state.js';

const ICONS = {
  finder: `<svg viewBox="0 0 100 100" aria-hidden="true"><path d="M15 34a7 7 0 0 1 7-7h22l8 9h32a7 7 0 0 1 7 7v34a7 7 0 0 1-7 7H22a7 7 0 0 1-7-7z" fill="#087be0"/><path d="M15 46h76l-6 32a8 8 0 0 1-8 6H23a8 8 0 0 1-8-6z" fill="#28a5fa"/><path d="M15 45h76v10H15z" fill="#e1f5ff"/><path d="m43 45 5 10H35l-5-10z" fill="#1492e9"/></svg>`,
  youtube: `<svg viewBox="0 0 100 100" aria-hidden="true"><rect x="5" y="22" width="90" height="60" rx="21" fill="#f40016"/><path d="m42 35 28 17-28 17z" fill="white"/></svg>`,
  tiktok: `<svg viewBox="0 0 100 100" aria-hidden="true"><path d="M52 15v48a16 16 0 1 1-13-16" fill="none" stroke="#ff1754" stroke-width="13" stroke-linecap="round"/><path d="M52 15c2 14 12 22 27 22" fill="none" stroke="#ff1754" stroke-width="13" stroke-linecap="round"/><path d="M48 12v48a16 16 0 1 1-13-16" fill="none" stroke="#00e6ef" stroke-width="13" stroke-linecap="round"/><path d="M48 12c2 14 12 22 27 22" fill="none" stroke="#00e6ef" stroke-width="13" stroke-linecap="round"/><path d="M50 14v48a16 16 0 1 1-13-16" fill="none" stroke="white" stroke-width="11" stroke-linecap="round"/><path d="M50 14c2 14 12 22 27 22" fill="none" stroke="white" stroke-width="11" stroke-linecap="round"/></svg>`,
  spotify: `<svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="43" fill="#16d879"/><g fill="none" stroke="#092b20" stroke-width="8" stroke-linecap="round"><path d="M26 36c19-5 36-3 49 4"/><path d="M28 51c17-4 31-2 43 4"/><path d="M30 65c14-3 26-1 37 3"/></g></svg>`,
  steam: `<svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="39" fill="#102c4a"/><path d="m22 60 26 16M47 45l25-16" stroke="#bdc9d5" stroke-width="9" stroke-linecap="round"/><circle cx="28" cy="66" r="9" fill="#bdc9d5"/><circle cx="28" cy="66" r="4" fill="#34516d"/><circle cx="73" cy="29" r="12" fill="#bdc9d5"/><circle cx="73" cy="29" r="7" fill="#102c4a"/><circle cx="52" cy="49" r="9" fill="#bdc9d5"/><circle cx="52" cy="49" r="5" fill="#34516d"/></svg>`,
  chrome: `<svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="43" fill="#e8edf5"/><path d="M50 7a43 43 0 0 1 37 22H50a22 22 0 0 0-19 11L15 20A43 43 0 0 1 50 7" fill="#ea4335"/><path d="M87 29a43 43 0 0 1-37 64l19-33a22 22 0 0 0 0-22z" fill="#fbbc05"/><path d="M50 93A43 43 0 0 1 15 20l19 33a22 22 0 0 0 35 7z" fill="#34a853"/><circle cx="50" cy="50" r="20" fill="#4285f4" stroke="white" stroke-width="4"/></svg>`,
  bilibili: `<svg viewBox="0 0 100 100" aria-hidden="true"><rect x="10" y="25" width="80" height="60" rx="16" fill="#FB7299"/><path d="m28 15 8 10M72 15l-8 10" stroke="#FB7299" stroke-width="6" stroke-linecap="round"/><rect x="22" y="42" width="12" height="18" rx="4" fill="white"/><rect x="66" y="42" width="12" height="18" rx="4" fill="white"/></svg>`,
  qqmusic: `<svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="43" fill="#31C27C"/><path d="M62 22v38a12 12 0 1 1-10-12V32l22-6v-4z" fill="white"/></svg>`,
  netease: `<svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="43" fill="#C20C0C"/><path d="M50 25c-8 0-14 6-14 14s6 14 14 14 14-6 14-14-6-14-14-14zm0 20a6 6 0 1 1 0-12 6 6 0 0 1 0 12z" fill="white"/><path d="M50 53v22M36 53l14 10 14-10" stroke="white" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  tencentvideo: `<svg viewBox="0 0 100 100" aria-hidden="true"><rect x="10" y="22" width="80" height="56" rx="14" fill="#FF5C00"/><path d="m40 32 28 18-28 18z" fill="white"/></svg>`,
  generic: `<svg viewBox="0 0 100 100" aria-hidden="true"><rect x="15" y="15" width="70" height="70" rx="17" fill="#8fa6db"/><path d="m43 32 27 18-27 18z" fill="white"/></svg>`,
};

function iconKind(app) {
  const text = `${app.icon || ''} ${app.name || ''} ${app.path || ''}`.toLowerCase();
  if (/youtube|youtu\.be/.test(text)) return 'youtube';
  if (/tiktok|douyin|抖音/.test(text)) return 'tiktok';
  if (/bilibili|哔哩哔哩/.test(text)) return 'bilibili';
  if (/qqmusic|qq音乐/.test(text)) return 'qqmusic';
  if (/netease|cloudmusic|网易云/.test(text)) return 'netease';
  if (/tencentvideo|qqlive|腾讯视频/.test(text)) return 'tencentvideo';
  if (/spotify/.test(text)) return 'spotify';
  if (/steam/.test(text)) return 'steam';
  if (/chrome/.test(text)) return 'chrome';
  if (/explorer|finder|文件/.test(text)) return 'finder';
  return 'generic';
}

export class AppCarousel {
  constructor(element, state, { onToast }) {
    this.element = element;
    this.track = element.querySelector('#carousel-track');
    this.state = state;
    this.onToast = onToast;
    this.drag = null;
    this.cards = [];
    this.render();
    state.addEventListener('config', () => this.render());
    state.addEventListener('apps', () => this.render());
    // 长按锁定状态只影响蓝框，重渲染卡片层级（不重建 DOM）。
    state.addEventListener('lock', () => this.render());
    // 轮播容器需要响应 touch-action，否则会与 iOS 页面回弹冲突。
    element.style.touchAction = 'pan-y';
    element.addEventListener('pointerdown', event => this.pointerDown(event));
    element.addEventListener('pointermove', event => this.pointerMove(event));
    element.addEventListener('pointerup', event => this.pointerUp(event));
    element.addEventListener('pointercancel', () => this.cancel());
    element.addEventListener('dragstart', event => event.preventDefault());
    element.addEventListener('keydown', event => {
      if (this.state.isLocked) return;
      if (event.key === 'ArrowLeft') { event.preventDefault(); this.move(-1); }
      if (event.key === 'ArrowRight') { event.preventDefault(); this.move(1); }
    });
  }

  render() {
    const apps = this.state.config.apps;
    const selected = this.state.selectedApp;
    const signature = apps.map(app => `${app.id}:${app.name}:${app.icon}:${app.icon_url || ''}:${app.card_art || ART_BY_ID[app.id] || ''}:${app.has_local || ''}:${app.route || ''}`).join('|');
    if (signature !== this.signature) {
      this.signature = signature;
      this.track.replaceChildren();
      this.cards = apps.map((app, index) => {
        const kind = iconKind(app);
        const button = document.createElement('button');
        button.className = 'app-card';
        button.type = 'button';
        button.dataset.icon = kind;
        button.style.setProperty('--app-color', app.color || '#1c2438');
        button.setAttribute('aria-label', `${app.name}，点击切换`);
        button.setAttribute('aria-roledescription', '应用卡片');
        button.dataset.index = String(index);
        const symbol = document.createElement('span');
        symbol.className = 'app-symbol';
        const art = app.card_art || ART_BY_ID[app.id];
        const iconUrl = app.icon_url || (typeof app.icon === 'string' && /^(https?:|data:image\/)/.test(app.icon) ? app.icon : null);
        if (art) {
          // 用户提供的整张卡片素材：铺满卡片（含自带渐变底、圆角、阴影与品牌图标），不再叠加品牌渐变底。
          const image = document.createElement('img');
          image.className = 'app-art';
          image.src = art;
          image.alt = '';
          image.draggable = false;
          image.addEventListener('error', () => {
            symbol.replaceChildren();
            const fallback = document.createElement('span');
            fallback.className = 'app-fallback-letter';
            fallback.textContent = (app.name || '?').slice(0, 1);
            fallback.style.background = app.color || '#33405b';
            symbol.append(fallback);
          });
          symbol.append(image);
          button.classList.add('has-art');
        } else if (iconUrl) {
          const image = document.createElement('img');
          image.className = 'app-image';
          image.src = iconUrl;
          image.alt = '';
          image.draggable = false;
          image.addEventListener('error', () => {
            symbol.replaceChildren();
            const fallback = document.createElement('span');
            fallback.className = 'app-fallback-letter';
            fallback.textContent = (app.name || '?').slice(0, 1);
            fallback.style.background = app.color || '#33405b';
            symbol.append(fallback);
          });
          symbol.append(image);
        } else if (ICONS[kind] && kind !== 'generic') {
          symbol.innerHTML = ICONS[kind];
        } else {
          const fallback = document.createElement('span');
          fallback.className = 'app-fallback-letter';
          fallback.textContent = (app.name || '?').slice(0, 1);
          fallback.style.background = app.color || '#33405b';
          symbol.append(fallback);
        }
        button.append(symbol);
        button.addEventListener('click', event => {
          if (this.suppressClick) { event.preventDefault(); return; }
          if (index !== this.state.selectedApp) {
            if (this.state.isLocked) this.onToast?.('已锁定应用，长按中央键解锁后再切换');
            else this.state.selectApp(index);
          }
        });
        this.track.append(button);
        return button;
      });
    }
    this.cards.forEach((card, index) => {
      let offset = index - selected;
      const count = apps.length;
      if (count >= 5) {
        if (offset > count / 2) offset -= count;
        if (offset < -count / 2) offset += count;
      }
      card.style.setProperty('--offset', String(offset));
      // 写入 DOM 属性，供 CSS 的 [data-offset] 显式层级兜底命中（Safari 对 calc(z-index) 支持不一）。
      card.dataset.offset = String(offset);
      card.classList.toggle('active', index === selected);
      // 只有被长按锁定（app.activate 成功 / 设置页锁定）的应用卡才显示蓝色边框。
      card.classList.toggle('locked', Boolean(this.state.lockedAppId) && apps[index]?.id === this.state.lockedAppId);
      card.tabIndex = index === selected ? 0 : -1;
      card.setAttribute('aria-current', index === selected ? 'true' : 'false');
      card.hidden = Math.abs(offset) > 2;
    });
  }

  pointerDown(event) {
    if (event.button !== 0 || this.drag || this.state.isLocked) return;
    this.drag = { id: event.pointerId, x: event.clientX, y: event.clientY, dx: 0, moved: false };
    try { this.element.setPointerCapture(event.pointerId); } catch { /* 失效指针时仍按位移判断横滑 */ }
  }

  pointerMove(event) {
    if (!this.drag || this.drag.id !== event.pointerId) return;
    this.drag.dx = event.clientX - this.drag.x;
    if (Math.abs(this.drag.dx) > 8) this.drag.moved = true;
    if (this.drag.moved) event.preventDefault();
    this.cards.forEach(card => {
      const offset = Number(card.style.getPropertyValue('--offset'));
      card.style.transition = 'none';
      // 与 CSS 堆叠公式逐项对齐：位移 55%、中央上浮 3%、scale 1.12-|offset|*.13、translateZ 20px 级差，避免拖动前后跳变。
      const abs = Math.min(Math.abs(offset), 2);
      card.style.transform = `translate(-50%,-50%) translateX(calc(${offset * 55}% + ${this.drag.dx}px)) translateY(${abs * 3 - 3}%) translateZ(${(2 - abs) * 20}px) scale(${1.12 - abs * .13})`;
    });
  }

  pointerUp(event) {
    if (!this.drag || this.drag.id !== event.pointerId) return;
    const { dx, moved } = this.drag;
    this.drag = null;
    this.suppressClick = moved;
    this.cards.forEach(card => { card.style.transition = ''; card.style.transform = ''; });
    if (moved && Math.abs(dx) > 30) {
      const sign = this.state.preferences.reverse_swipe ? -1 : 1;
      this.move((dx < 0 ? 1 : -1) * sign);
    }
    setTimeout(() => { this.suppressClick = false; }, 80);
  }

  cancel() {
    this.drag = null;
    this.cards.forEach(card => { card.style.transition = ''; card.style.transform = ''; });
  }

  move(direction) {
    this.state.moveApp(direction);
  }
}
