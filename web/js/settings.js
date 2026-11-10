import { newId } from './state.js';
import { TEMPLATES, applyTemplate } from './templates.js';

const KEY_NAMES = { Control: 'ctrl', Shift: 'shift', Alt: 'alt', Meta: 'win', Enter: 'enter', Escape: 'esc', ' ': 'space', Backspace: 'backspace', Delete: 'delete', ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', CapsLock: 'capslock', Insert: 'insert', PrintScreen: 'printscreen', Tab: 'tab', Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown' };

const PICKER_MODIFIERS = ['ctrl', 'shift', 'alt', 'win'];
const PICKER_KEYS = [
  ...'abcdefghijklmnopqrstuvwxyz',
  ...'0123456789',
  ...Array.from({ length: 12 }, (_, i) => `f${i + 1}`),
  'enter', 'esc', 'tab', 'space', 'backspace', 'delete',
  'up', 'down', 'left', 'right', 'home', 'end', 'pageup', 'pagedown', 'insert', 'printscreen', 'capslock',
  '`', '-', '=', '[', ']', '\\', ';', "'", ',', '.', '/',
];

export class Settings {
  constructor(state, connection, { onToast, onLock }) {
    this.state = state;
    this.connection = connection;
    this.onToast = onToast;
    this.onLock = onLock;
    this.dialog = document.querySelector('#settings-dialog');
    this.recordedKeys = [];
    this.recording = false;
    this.recordingCleanup = null;
    this.apps = [];
    this.bind();
    state.addEventListener('config', () => { this.fillGeneral(); this.renderHotkeys(); this.renderPages(); this.renderApps(); });
  }

  bind() {
    document.querySelector('#settings-open').addEventListener('click', () => this.open());
    document.querySelector('#settings-close').addEventListener('click', () => this.close());
    this.dialog.addEventListener('close', () => this.stopRecording());
    this.dialog.addEventListener('click', event => {
      if (event.target === this.dialog) {
        const rect = this.dialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) this.close();
      }
    });
    document.querySelectorAll('[data-settings-tab]').forEach(button => button.addEventListener('click', () => {
      document.querySelectorAll('[data-settings-tab]').forEach(item => item.classList.toggle('active', item === button));
      document.querySelectorAll('.settings-panel').forEach(panel => { panel.hidden = panel.id !== `settings-${button.dataset.settingsTab}`; });
      this.cancelForms();
      if (button.dataset.settingsTab === 'apps') this.loadApps();
    }));
    this.bindRanges();
    this.bindToggle('#setting-natural', 'natural_scroll');
    this.bindToggle('#setting-swipe', 'reverse_swipe');
    document.querySelector('#setting-lock').addEventListener('change', event => {
      const locked = event.target.checked;
      const app = this.state.selectedAppData;
      if (locked && app) this.connection.send('app.activate', { id: app.id });
      else this.connection.send('app.release', {});
      this.state.setLocked(locked ? app?.id : null);
      this.onLock?.();
    });
    document.querySelector('#fullscreen-button').addEventListener('click', async () => {
      try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await document.documentElement.requestFullscreen();
        this.status(document.fullscreenElement ? '已进入全屏' : '浏览器已退出全屏');
      } catch { this.status('当前 Safari 版本不提供网页全屏，可从分享菜单添加到主屏幕。', true); }
    });
    document.querySelector('#settings-reconnect').addEventListener('click', () => { this.connection.reconnect(); this.status('正在重新连接…'); });
    this.buildKeyPicker();
    this.renderTemplates();
    document.querySelector('#hotkey-add').addEventListener('click', () => this.editHotkey());
    document.querySelector('#hotkey-cancel').addEventListener('click', () => this.cancelForms());
    document.querySelector('#hotkey-record').addEventListener('click', () => this.startRecording());
    document.querySelector('#hotkey-form').addEventListener('submit', event => this.saveHotkey(event));
    document.querySelector('#page-add').addEventListener('click', () => this.editPage());
    document.querySelector('#page-cancel').addEventListener('click', () => this.cancelForms());
    document.querySelector('#page-form').addEventListener('submit', event => this.savePage(event));
    document.querySelector('#apps-refresh').addEventListener('click', () => this.loadApps(true));
  }

  buildKeyPicker() {
    const picker = document.querySelector('#key-picker');
    if (!picker) return;
    picker.replaceChildren();
    const modifiers = document.createElement('div');
    modifiers.className = 'key-picker-group';
    PICKER_MODIFIERS.forEach(key => modifiers.append(this.keyCap(key, true)));
    const keys = document.createElement('div');
    keys.className = 'key-picker-group';
    PICKER_KEYS.forEach(key => keys.append(this.keyCap(key, false)));
    picker.append(modifiers, keys);
  }

  keyCap(key, isModifier) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'key-cap';
    button.dataset.key = key;
    button.dataset.modifier = isModifier ? '1' : '';
    button.textContent = key === ' ' ? 'space' : key;
    button.addEventListener('click', () => {
      const set = new Set(this.recordedKeys);
      if (set.has(key)) set.delete(key);
      else {
        if (!isModifier) for (const item of [...set]) if (!PICKER_MODIFIERS.includes(item)) set.delete(item);
        set.add(key);
      }
      this.recordedKeys = [...PICKER_MODIFIERS.filter(m => set.has(m)), ...[...set].filter(k => !PICKER_MODIFIERS.includes(k))];
      this.updateRecorded();
    });
    return button;
  }

  renderTemplates() {
    const list = document.querySelector('#template-list');
    if (!list) return;
    list.replaceChildren();
    TEMPLATES.forEach(template => {
      const card = document.createElement('div');
      card.className = 'template-card';
      const keys = ['A', 'B', 'C'].map(slot => template.keys[slot] ? `${slot}=[${template.keys[slot].join('+')}]` : `${slot}=—`).join(' · ');
      card.innerHTML = `<strong>${template.name}</strong><small>${keys}</small><small class="template-note">${template.note}</small>`;
      const apply = document.createElement('button');
      apply.type = 'button';
      apply.className = 'primary-button template-apply';
      apply.textContent = '套用';
      apply.addEventListener('click', () => {
        const hotkeys = applyTemplate(template, this.state.config.active_page_id);
        const templateIds = new Set(hotkeys.map(item => `${item.slot}:${item.keys.join('+')}`));
        this.state.config.hotkeys = this.state.config.hotkeys.filter(item => !templateIds.has(`${item.slot}:${item.keys.join('+')}`));
        this.state.config.hotkeys.push(...hotkeys);
        this.renderHotkeys();
        this.saveConfig(`已套用「${template.name}」模板`);
      });
      card.append(apply);
      list.append(card);
    });
  }

  bindRanges() {
    const mappings = [
      ['sensitivity', 'move_sensitivity', value => Number(value).toFixed(1)],
      ['scroll', 'scroll_speed', value => Number(value).toFixed(1)],
      ['double-tap', 'double_click_ms', value => `${value} ms`],
      ['lock-hold', 'lock_hold_ms', value => `${value} ms`],
    ];
    mappings.forEach(([id, key, format]) => {
      const input = document.querySelector(`#setting-${id}`);
      input.addEventListener('input', () => {
        document.querySelector(`#${id}-value`).textContent = format(input.value);
        this.state.touchpad[key] = Number(input.value);
      });
      input.addEventListener('change', () => this.saveConfig(`${key} 已保存`));
    });
  }

  bindToggle(selector, key) {
    const input = document.querySelector(selector);
    input.checked = this.state.preferences[key];
    input.addEventListener('change', () => this.state.setPreference(key, input.checked));
  }

  fillGeneral() {
    const values = { sensitivity: this.state.touchpad.move_sensitivity, scroll: this.state.touchpad.scroll_speed, 'double-tap': this.state.touchpad.double_click_ms, 'lock-hold': this.state.touchpad.lock_hold_ms };
    Object.entries(values).forEach(([id, value]) => {
      document.querySelector(`#setting-${id}`).value = value;
      document.querySelector(`#${id}-value`).textContent = id === 'double-tap' || id === 'lock-hold' ? `${value} ms` : Number(value).toFixed(1);
    });
    document.querySelector('#setting-natural').checked = this.state.preferences.natural_scroll;
    document.querySelector('#setting-swipe').checked = this.state.preferences.reverse_swipe;
    document.querySelector('#setting-lock').checked = this.state.isLocked;
  }

  open(tab = 'general') {
    this.fillGeneral();
    this.renderHotkeys();
    this.renderPages();
    if (!this.dialog.open) this.dialog.showModal();
    document.querySelector(`[data-settings-tab="${tab}"]`).click();
  }

  close() { this.stopRecording(); this.dialog.close(); }

  status(text, error = false) {
    const status = document.querySelector('#settings-status');
    status.textContent = text;
    status.classList.toggle('error', error);
  }

  async saveConfig(message) {
    const snapshot = structuredClone(this.state.config);
    try {
      await this.connection.saveConfig(snapshot);
      this.state.setConfig(snapshot);
      this.status(message);
    } catch (error) {
      this.status(`保存未完成：${error.message}`, true);
      this.onToast('演示态仅更新本机预览');
    }
  }

  renderHotkeys() {
    const list = document.querySelector('#hotkey-list');
    list.replaceChildren();
    if (!this.state.config.hotkeys.length) {
      list.innerHTML = '<div class="empty-state">还没有快捷键。新增后，可将它分配到页面的 A / B / C 位置。</div>';
      return;
    }
    this.state.config.hotkeys.forEach(hotkey => {
      const page = this.state.config.pages.find(item => item.id === hotkey.page_id);
      const row = this.row(hotkey.name || '未命名', `${hotkey.keys.join(' + ')} · ${page?.name || '未分配页面'}${hotkey.slot ? ` · ${hotkey.slot}` : ''}`);
      row.querySelector('[data-edit]').addEventListener('click', () => this.editHotkey(hotkey));
      row.querySelector('[data-delete]').addEventListener('click', () => {
        this.state.config.hotkeys = this.state.config.hotkeys.filter(item => item.id !== hotkey.id);
        this.saveConfig('快捷键已删除');
      });
      list.append(row);
    });
  }

  row(title, subtitle, active = false) {
    const row = document.createElement('div');
    row.className = `editor-row${active ? ' row-active' : ''}`;
    const main = document.createElement('div');
    main.className = 'row-main';
    const strong = document.createElement('strong');
    strong.textContent = title;
    const small = document.createElement('small');
    small.textContent = subtitle;
    main.append(strong, small);
    const actions = document.createElement('div');
    actions.className = 'row-actions';
    actions.innerHTML = '<button type="button" class="row-action" data-edit aria-label="编辑"><svg class="icon"><use href="#i-edit"/></svg></button><button type="button" class="row-action danger" data-delete aria-label="删除"><svg class="icon"><use href="#i-trash"/></svg></button>';
    row.append(main, actions);
    return row;
  }

  editHotkey(hotkey = null) {
    this.cancelForms();
    const form = document.querySelector('#hotkey-form');
    form.hidden = false;
    document.querySelector('#hotkey-id').value = hotkey?.id || '';
    document.querySelector('#hotkey-name').value = hotkey?.name || '';
    this.recordedKeys = [...(hotkey?.keys || [])];
    this.updateRecorded();
    document.querySelector('#hotkey-name').focus();
  }

  startRecording() {
    this.stopRecording();
    this.recording = true;
    this.recordedKeys = [];
    const button = document.querySelector('#hotkey-record');
    button.textContent = '请按下组合键…';
    const listener = event => {
      event.preventDefault();
      event.stopPropagation();
      const key = this.normalizeKey(event);
      if (!key) return;
      if (event.type === 'keyup') {
        this.stopRecording();
        return;
      }
      const keys = [];
      if (event.ctrlKey) keys.push('ctrl');
      if (event.shiftKey) keys.push('shift');
      if (event.altKey) keys.push('alt');
      if (event.metaKey) keys.push('win');
      if (!keys.includes(key)) keys.push(key);
      this.recordedKeys = [...new Set(keys)];
      this.updateRecorded();
      if (!['ctrl', 'shift', 'alt', 'win'].includes(key)) this.stopRecording();
    };
    window.addEventListener('keydown', listener, true);
    window.addEventListener('keyup', listener, true);
    this.recordingCleanup = () => {
      window.removeEventListener('keydown', listener, true);
      window.removeEventListener('keyup', listener, true);
    };
  }

  normalizeKey(event) {
    if (KEY_NAMES[event.key]) return KEY_NAMES[event.key];
    if (/^[a-z0-9]$/i.test(event.key)) return event.key.toLowerCase();
    if (/^F([1-9]|1[0-2])$/i.test(event.key)) return event.key.toLowerCase();
    if (['`', '-', '=', '[', ']', '\\', ';', "'", ',', '.', '/'].includes(event.key)) return event.key;
    return null;
  }

  stopRecording() {
    this.recordingCleanup?.();
    this.recordingCleanup = null;
    this.recording = false;
    const button = document.querySelector('#hotkey-record');
    if (button) button.textContent = '点击录制组合键';
  }

  updateRecorded() {
    document.querySelector('#hotkey-recorded').textContent = this.recordedKeys.length ? this.recordedKeys.join(' + ') : '尚未选择按键';
    document.querySelectorAll('#key-picker .key-cap').forEach(cap => {
      cap.classList.toggle('selected', this.recordedKeys.includes(cap.dataset.key));
    });
  }

  saveHotkey(event) {
    event.preventDefault();
    if (!this.recordedKeys.length) { this.status('请先录制至少一个按键。', true); return; }
    const id = document.querySelector('#hotkey-id').value;
    const old = this.state.config.hotkeys.find(item => item.id === id);
    const hotkey = { ...(old || {}), id: id || newId('hk'), name: document.querySelector('#hotkey-name').value.trim(), icon: old?.icon || '', keys: [...this.recordedKeys], page_id: old?.page_id || this.state.config.active_page_id || null, slot: old?.slot || null };
    if (id) this.state.config.hotkeys = this.state.config.hotkeys.map(item => item.id === id ? hotkey : item);
    else this.state.config.hotkeys.push(hotkey);
    this.cancelForms();
    this.saveConfig('快捷键已保存');
  }

  renderPages() {
    const list = document.querySelector('#page-list');
    list.replaceChildren();
    if (!this.state.config.pages.length) {
      list.innerHTML = '<div class="empty-state">新建页面并关联快捷键，即可在 A / B / C 区域快速执行。</div>';
      return;
    }
    this.state.config.pages.forEach(page => {
      const hotkeys = this.state.config.hotkeys.filter(item => item.page_id === page.id);
      const row = this.row(page.name, `${hotkeys.length} 个快捷键${this.state.config.active_page_id === page.id ? ' · 当前页面' : ''}`, this.state.config.active_page_id === page.id);
      const main = row.querySelector('.row-main');
      main.role = 'button';
      main.tabIndex = 0;
      main.setAttribute('aria-label', `切换到${page.name}`);
      const activate = () => {
        this.state.config.active_page_id = page.id;
        this.state.activePageId = page.id;
        this.saveConfig(`已切换到${page.name}`);
      };
      main.addEventListener('click', activate);
      main.addEventListener('keydown', event => { if (event.key === 'Enter') activate(); });
      row.querySelector('[data-edit]').addEventListener('click', () => this.editPage(page));
      row.querySelector('[data-delete]').addEventListener('click', () => {
        this.state.config.pages = this.state.config.pages.filter(item => item.id !== page.id);
        this.state.config.hotkeys = this.state.config.hotkeys.map(item => item.page_id === page.id ? { ...item, page_id: null, slot: null } : item);
        if (this.state.config.active_page_id === page.id) this.state.config.active_page_id = this.state.config.pages[0]?.id || null;
        this.saveConfig('页面已删除，原快捷键保留为未分配');
      });
      list.append(row);
    });
  }

  editPage(page = null) {
    this.cancelForms();
    const form = document.querySelector('#page-form');
    form.hidden = false;
    document.querySelector('#page-id').value = page?.id || '';
    document.querySelector('#page-name').value = page?.name || '';
    document.querySelector('#page-app').value = page?.app_id || '';
    const select = document.querySelector('#page-hotkeys');
    select.replaceChildren();
    this.state.config.hotkeys.forEach(hotkey => {
      const option = document.createElement('option');
      option.value = hotkey.id;
      option.textContent = `${hotkey.name}（${hotkey.keys.join('+')}）`;
      option.selected = hotkey.page_id === page?.id;
      select.append(option);
    });
    document.querySelector('#app-name-options').replaceChildren();
    this.state.config.apps.forEach(app => {
      const option = document.createElement('option');
      option.value = app.id;
      option.label = app.name;
      document.querySelector('#app-name-options').append(option);
    });
    document.querySelector('#page-name').focus();
  }

  savePage(event) {
    event.preventDefault();
    const id = document.querySelector('#page-id').value;
    const page = { id: id || newId('pg'), name: document.querySelector('#page-name').value.trim(), icon: '', app_id: document.querySelector('#page-app').value.trim() || null };
    if (id) this.state.config.pages = this.state.config.pages.map(item => item.id === id ? { ...item, ...page } : item);
    else this.state.config.pages.push(page);
    const selected = new Set([...document.querySelector('#page-hotkeys').selectedOptions].map(option => option.value));
    const usedSlots = new Set();
    this.state.config.hotkeys = this.state.config.hotkeys.map(hotkey => {
      if (!selected.has(hotkey.id)) return hotkey.page_id === page.id ? { ...hotkey, page_id: null, slot: null } : hotkey;
      const preferred = hotkey.slot && !usedSlots.has(hotkey.slot) ? hotkey.slot : ['A', 'B', 'C'].find(slot => !usedSlots.has(slot));
      if (preferred) usedSlots.add(preferred);
      return { ...hotkey, page_id: page.id, slot: preferred || null };
    });
    if (!this.state.config.active_page_id) this.state.config.active_page_id = page.id;
    this.cancelForms();
    this.saveConfig('页面已保存');
  }

  cancelForms() {
    this.stopRecording();
    document.querySelector('#hotkey-form').hidden = true;
    document.querySelector('#page-form').hidden = true;
  }

  async loadApps(scan = false) {
    this.status('正在读取应用列表…');
    try {
      const result = scan ? await this.connection.request('/api/apps/scan', { method: 'POST', body: '{}' }) : await this.connection.getApps();
      this.apps = Array.isArray(result.apps) ? result.apps : [];
      this.renderApps();
      this.status(`已读取 ${this.apps.length} 个应用`);
    } catch (error) {
      this.apps = [...this.state.config.apps];
      this.renderApps();
      this.status(`演示应用列表：${error.message}`, true);
    }
  }

  renderApps() {
    const list = document.querySelector('#app-list');
    list.replaceChildren();
    const source = this.apps.length ? this.apps : this.state.config.apps;
    if (!source.length) { list.innerHTML = '<div class="empty-state">未发现可添加的应用。</div>'; return; }
    source.forEach(app => {
      const added = this.state.config.apps.some(item => item.id === app.id || (item.path && item.path === app.path));
      const row = this.row(app.name, `${app.path || '已配置应用'}${added ? ' · 已在应用页' : ''}`);
      const icon = document.createElement('span');
      icon.className = 'app-mini-icon';
      icon.style.setProperty('--app-color', app.color || '#33405b');
      if (app.icon_url) {
        const image = document.createElement('img');
        image.src = app.icon_url;
        image.alt = '';
        image.addEventListener('error', () => { image.remove(); icon.textContent = app.name.slice(0, 1); });
        icon.append(image);
      } else {
        icon.textContent = app.name.slice(0, 1);
      }
      row.prepend(icon);
      row.querySelector('[data-edit]').setAttribute('aria-label', added ? '已添加' : '添加到应用页');
      row.querySelector('[data-edit]').innerHTML = `<svg class="icon"><use href="#${added ? 'i-check' : 'i-plus'}"/></svg>`;
      row.querySelector('[data-edit]').disabled = added;
      row.querySelector('[data-edit]').addEventListener('click', async () => {
        try {
          await this.connection.addApp(app);
          if (!added) {
            const stored = { ...app };
            if (!stored.icon_url && stored.path) {
              stored.icon_url = `/api/apps/icon?path=${encodeURIComponent(stored.path)}`;
            }
            this.state.config.apps.push(stored);
          }
          this.state.emit('config', this.state.config);
          await this.saveConfig(`已添加${app.name}`);
          try {
            const result = await this.connection.getApps();
            if (Array.isArray(result.apps)) { this.apps = result.apps; this.renderApps(); }
          } catch { /* 回填失败时保留本地图标规则。 */ }
        } catch (error) {
          this.status(`添加未完成：${error.message}`, true);
        }
      });
      row.querySelector('[data-delete]').addEventListener('click', () => {
        this.state.config.apps = this.state.config.apps.filter(item => item.id !== app.id);
        if (!this.state.config.apps.length) this.state.config.apps.push(app);
        this.saveConfig(`已从应用页移除${app.name}`);
      });
      list.append(row);
    });
  }
}
